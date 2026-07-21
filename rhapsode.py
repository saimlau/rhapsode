#!/usr/bin/env python3
"""Rhapsode — turn academic paper PDFs into narrated read-along audio.

Extracts title, abstract, and body text in reading order (PyMuPDF),
skips affiliations / page furniture / figure captions / References,
strips citation brackets, and synthesizes speech locally with Kokoro
(CUDA when available). `--play` builds and opens a browser read-along
view with synced sentence highlighting; `--gui` starts the library web
app.
"""

import argparse
import gc
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import fitz  # PyMuPDF

from config import load_config, library_path
from extraction import (MappedText, clean_mapped, clean_text,
                        extract_segments, merge_continuations, split_sentences)

SAMPLE_RATE = 24000
# A long paper is thousands of TTS requests; without retry, any per-request
# failure rate makes a complete run improbable, and a failure costs the paper.
TTS_ATTEMPTS = 4
TTS_BACKOFF = 0.5      # seconds; doubled per attempt, jittered
HEADING_PAUSE_S = 0.7
PARAGRAPH_PAUSE_S = 0.35
SENTENCE_PAUSE_S = 0.08

_PIPELINE = None  # warm Kokoro model, reused across papers in one process
_PIPE_STATE = {"last_used": 0.0, "parked": False}
TTS_LOCK = threading.Lock()  # one inference at a time (worker + /tts
                             # endpoint) and all pipeline state transitions


def prepare_units(pdf_path, grobid_cfg=None, llm_cfg=None):
    """Extract and clean a paper. Returns (units, meta, warnings); units are
    {kind, text, rects, para_end, pause}.

    With [llm] enabled and a runner available, LLM block-classification is the
    primary extractor: it reads each PDF block's location and first/last
    sentence, keeps and orders the content blocks, and emits their own text
    (so nothing is invented) with rectangles from the block word boxes. It
    recovers body text GROBID drops around footnotes/column breaks. GROBID (or
    the built-in heuristics) is the fallback when the LLM is off or fails.

    Raises ValueError for PDFs with no usable text (scanned/image-only).
    """
    if llm_cfg and llm_cfg.get("enabled"):
        import llm as llm_mod
        runner = llm_mod.resolve(llm_cfg)
        if runner:
            try:
                import reflow
                units, meta = reflow.extract_document(pdf_path, llm_cfg)
                body = [u for u in units if u["kind"] == "body"]
                words = sum(len(u["text"].split()) for u in body)
                if len(body) >= 3 and words >= 300:
                    meta = _resolve_meta(pdf_path, meta)
                    return units, meta, [f"extracted via LLM ({runner}); "
                                         f"metadata via heuristic parser"]
                fb = f"LLM extraction returned too little ({words} words); "
            except Exception as e:
                fb = f"LLM extraction failed ({type(e).__name__}: {e}); "
        else:
            fb = ("LLM enabled but no runner available (install ollama/claude/"
                  "codex or set [llm] api_key); ")
        units, meta, warnings = _base_extract(pdf_path, grobid_cfg)
        return units, meta, [fb + "used base extractor"] + warnings

    return _base_extract(pdf_path, grobid_cfg)


def _resolve_meta(pdf_path, llm_meta):
    """Metadata for the LLM-body path. The block classifier truncates long
    titles and misses authors in long lists (it only sees each block's
    first/last sentence), so prefer the tuned heuristic front-matter parser,
    with the LLM's values and a page-scan for the year as fallbacks."""
    meta = dict(llm_meta or {})
    try:
        from extraction import extract_segments
        _, _, hm = extract_segments(pdf_path)
        for k in ("title", "authors", "year"):
            if hm.get(k):
                meta[k] = hm[k]
    except Exception:
        pass
    if meta.get("year") is None:
        try:
            from extraction import _page_year
            meta["year"] = _page_year(fitz.open(pdf_path)[0])
        except Exception:
            pass
    return meta


def _base_extract(pdf_path, grobid_cfg=None):
    if grobid_cfg and grobid_cfg.get("enabled"):
        import grobid
        try:
            if grobid.ensure(grobid_cfg["url"],
                             home=grobid_cfg.get("home"),
                             autostart=grobid_cfg.get("autostart", True)):
                units, meta, warnings = grobid.extract(pdf_path,
                                                       grobid_cfg["url"])
                for u in units:
                    u["text"] = clean_text(u["text"])
                    u["pause"] = (HEADING_PAUSE_S if u["kind"] == "heading"
                                  else PARAGRAPH_PAUSE_S if u["para_end"]
                                  else SENTENCE_PAUSE_S)
                units = [u for u in units if u["text"]]
                if meta.get("year") is None:
                    from extraction import _page_year
                    meta["year"] = _page_year(fitz.open(pdf_path)[0])
                return units, meta, warnings
            warnings = ["GROBID unavailable; using built-in extraction"]
        except ValueError:
            raise
        except Exception as e:
            warnings = [f"GROBID failed ({type(e).__name__}: {e}); "
                        f"using built-in extraction"]
    else:
        warnings = []

    segments, found_references, meta = extract_segments(pdf_path)
    segments = merge_continuations(segments)
    while segments and segments[-1][0] == "heading":
        segments.pop()  # orphan trailing heading (e.g. Declarations with small-font body)
    if sum(len(mt) for _, mt in segments) < 500:
        raise ValueError("almost no text extracted — is this a scanned/image-only PDF?")
    if not found_references:
        warnings.append("no References heading found; reading to the end of the PDF")

    units = []
    for kind, mt in segments:
        cleaned = clean_mapped(mt)
        if not cleaned.text:
            continue
        if kind == "heading":
            units.append({"kind": "heading", "text": cleaned.text,
                          "rects": cleaned.rects(), "para_end": False,
                          "pause": HEADING_PAUSE_S})
        else:
            sentences = split_sentences(cleaned)
            for j, sentence in enumerate(sentences):
                last = j == len(sentences) - 1
                units.append({"kind": "body", "text": sentence.text,
                              "rects": sentence.rects(), "para_end": last,
                              "pause": PARAGRAPH_PAUSE_S if last
                              else SENTENCE_PAUSE_S})
    return units, meta, warnings


def make_tags(pdf_path, meta):
    title = clean_text(meta["title"] or "") or pdf_path.stem
    artist = (f"{meta['authors']} (audio by Rhapsode)" if meta["authors"]
              else "audio by Rhapsode")
    return {"title": title, "artist": artist}


# ----------------------------------------------------------------- synthesis

def get_pipeline():
    """Load Kokoro on demand; queued papers reuse the warm model. The whole
    check-load-unpark sequence holds TTS_LOCK so concurrent first callers
    (worker + /tts) can't double-load."""
    global _PIPELINE
    with TTS_LOCK:
        if _PIPELINE is None:
            import torch
            from kokoro import KPipeline
            mps = (getattr(torch.backends, "mps", None) is not None
                   and torch.backends.mps.is_available())
            device = ("cuda" if torch.cuda.is_available()
                      else "mps" if mps else "cpu")
            if device == "cpu":
                print("warning: no GPU available, synthesizing on CPU (slower)")
            try:
                _PIPELINE = KPipeline(lang_code="a",
                                      repo_id="hexgrad/Kokoro-82M",
                                      device=device)
            except Exception:
                if device != "mps":
                    raise
                print("warning: MPS backend failed, falling back to CPU")
                device = "cpu"
                _PIPELINE = KPipeline(lang_code="a",
                                      repo_id="hexgrad/Kokoro-82M",
                                      device="cpu")
            _PIPE_STATE["device"] = device
            _PIPE_STATE["parked"] = False
        elif _PIPE_STATE["parked"]:
            dev = _PIPE_STATE.get("device", "cpu")
            if dev != "cpu":
                _PIPELINE.model.to(dev)
            _PIPE_STATE["parked"] = False
        _PIPE_STATE["last_used"] = time.time()
        return _PIPELINE


def maybe_idle_models(park_after_s, unload_after_s):
    """Tiered idle release, called periodically by the server's worker:
    park the model to CPU RAM after park_after_s (~0.05 s to resume),
    drop it entirely after unload_after_s (~2 s to rebuild). Holding
    TTS_LOCK means transitions can't race an in-flight synthesis."""
    global _PIPELINE
    with TTS_LOCK:
        if _PIPELINE is None:
            return "unloaded"
        import torch
        idle = time.time() - _PIPE_STATE["last_used"]
        def _free_cache():
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            elif _PIPE_STATE.get("device") == "mps":
                try:
                    torch.mps.empty_cache()
                except Exception:
                    pass
        if idle >= unload_after_s:
            _PIPELINE = None
            _PIPE_STATE["parked"] = False
            gc.collect()
            _free_cache()
            print(f"kokoro: unloaded after {idle:.0f}s idle")
            return "unloaded"
        if idle >= park_after_s and not _PIPE_STATE["parked"]:
            if _PIPE_STATE.get("device", "cpu") != "cpu":
                _PIPELINE.model.to("cpu")
                _free_cache()
            _PIPE_STATE["parked"] = True
            print(f"kokoro: parked to CPU after {idle:.0f}s idle")
        return "parked" if _PIPE_STATE["parked"] else "loaded"


def pipeline_status():
    if _PIPELINE is None:
        return {"state": "unloaded"}
    return {"state": "parked" if _PIPE_STATE["parked"] else "loaded",
            "idle_s": round(time.time() - _PIPE_STATE["last_used"], 1)}


def _local_unit_audio(units, voice, speed):
    """Yield (unit, [(float32 wave, chunk-relative words)]) from the local
    Kokoro pipeline. Word timestamps come straight from Kokoro's tokens."""
    pipeline = get_pipeline()
    for unit in units:
        with TTS_LOCK:
            results = list(pipeline(unit["text"], voice=voice, speed=speed))
        chunks = []
        for item in results:
            audio = getattr(item, "audio", None)
            if audio is None:
                _, _, audio = item
            words = [{"w": tok.text, "t0": round(tok.start_ts, 3),
                      "t1": round(tok.end_ts, 3)}
                     for tok in (getattr(item, "tokens", None) or [])
                     if getattr(tok, "start_ts", None) is not None]
            chunks.append((audio.detach().cpu().numpy(), words))
        yield unit, chunks


def _modal_unit_audio(units, voice, speed, tts_cfg, batch=8, lookahead=4):
    """Yield the same shape as _local_unit_audio, synthesized on the user's
    own Modal deployment (modal_app.py). The identical Kokoro code runs
    there, so word timestamps are the same in kind — read-along sync does
    not depend on where inference happens.

    Up to `lookahead` batches are in flight at once (the endpoint declares
    @modal.concurrent, so one warm container absorbs them), but results are
    yielded strictly in order: audio streams into ffmpeg sequentially, so
    out-of-order completion must never become out-of-order output. Keeping
    requests dense also stops the container idling into a cold start between
    batches. lookahead=1 restores fully serial behaviour."""
    import base64
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    import requests

    endpoint = (tts_cfg.get("modal_endpoint") or "").strip()
    if not endpoint:
        raise RuntimeError("[tts] backend='modal' but modal_endpoint is not "
                           "set in config.toml (deploy modal_app.py first)")
    # Modal proxy auth needs BOTH halves; half a pair yields an opaque 401
    tok_id = (tts_cfg.get("modal_token_id") or "").strip()
    tok_secret = (tts_cfg.get("modal_token_secret") or "").strip()
    if bool(tok_id) != bool(tok_secret):
        raise RuntimeError("[tts] modal_token_id and modal_token_secret must "
                           "both be set (Modal proxy auth needs the pair)")
    headers = ({"Modal-Key": tok_id, "Modal-Secret": tok_secret}
               if tok_id else {})
    session = requests.Session()
    groups = [units[i:i + batch] for i in range(0, len(units), batch)]

    # The endpoint hard-rejects texts over 2000 chars, and no extractor can
    # be trusted never to produce one (a thesis TOC classified as a heading,
    # an appendix run). Enforce here, at the choke point: split an oversized
    # unit at whitespace, synthesize the pieces, and stitch wave + word
    # timings back into ONE unit so the manifest and read-along see nothing.
    def _pieces(text, limit=1900):
        out = []
        while len(text) > limit:
            cut = text.rfind(" ", limit // 2, limit)
            cut = cut if cut > 0 else limit
            out.append(text[:cut])
            text = text[cut:].lstrip()
        if text:
            out.append(text)
        return out or [""]

    # flatten units -> pieces, then pack requests by PIECE count: the endpoint
    # caps a request at `batch` texts, so a split unit's pieces count against
    # the cap and may span requests; reassembly below is piece-order driven
    flat = []                            # (unit_index, piece_text)
    for ui, u in enumerate(units):
        for piece in _pieces(u["text"]):
            flat.append((ui, piece))
    groups = [flat[i:i + batch] for i in range(0, len(flat), batch)]

    def fetch(group):
        texts = [t for _, t in group]
        # A paper is thousands of requests; at any per-request failure rate a
        # run without retry eventually dies, and one death costs the whole
        # paper. The request is a pure function of `texts`, so retrying is
        # safe. Retry only TRANSPORT failures — a 200 carrying {"error": ...}
        # is deterministic and would just burn GPU credits on every attempt.
        for attempt in range(TTS_ATTEMPTS):
            try:
                # (connect, read): a wedged container must not pin shutdown
                # for ten minutes. A request is a handful of short texts.
                resp = session.post(endpoint, headers=headers,
                                    timeout=(10, 180),
                                    json={"texts": texts,
                                          "voice": voice, "speed": speed})
                resp.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", 0)
                if status < 500:            # 4xx is our bug, not a blip
                    raise
                last = exc
            if attempt == TTS_ATTEMPTS - 1:
                raise last
            # exponential backoff, jittered so parallel workers that hit the
            # same wedged container do not retry in lockstep
            time.sleep(TTS_BACKOFF * (2 ** attempt) * (1 + random.random()))

        data = resp.json()
        if data.get("error"):  # endpoint reports 200 + {"error": ...}
            raise RuntimeError(f"modal endpoint error: {data['error']}")
        results = data.get("results") or []
        if len(results) != len(texts):
            raise RuntimeError(f"modal endpoint returned {len(results)} "
                               f"results for {len(texts)} texts")
        return results

    width = max(1, int(lookahead))
    # NOT `with ThreadPoolExecutor(...)`: its __exit__ is shutdown(wait=True),
    # which would block the error path, the abandoned-generator path, and
    # interpreter exit on Ctrl+C until every in-flight POST returned.
    pool = ThreadPoolExecutor(max_workers=width)
    pending = {}

    def _finish(ui, chunk):
        """chunk = this unit's piece-results in order -> (unit, [(wave, words)])
        with PCM concatenated and word timings shifted past earlier pieces."""
        pcm_parts, words, offset = [], [], 0.0
        for res in chunk:
            raw = base64.b64decode(res["pcm_b64"])
            pcm_parts.append(raw)
            for w in (res.get("words") or []):
                words.append({**w, "t0": round(w["t0"] + offset, 3),
                              "t1": round(w["t1"] + offset, 3)})
            offset += len(raw) / 2 / SAMPLE_RATE
        wave = (np.frombuffer(b"".join(pcm_parts), dtype="<i2")
                .astype(np.float32) / 32767.0)
        return units[ui], [(wave, words)]

    try:
        submitted = 0
        hold_ui, hold = None, []       # pieces accumulated for the open unit
        for idx, group in enumerate(groups):
            while submitted < len(groups) and submitted < idx + width:
                pending[submitted] = pool.submit(fetch, groups[submitted])
                submitted += 1
            # .result() on THIS index: blocks until the request we owe next is
            # done, regardless of which finished first — and re-raises its
            # exception here, so a failing request still aborts the paper
            results = pending.pop(idx).result()
            for (ui, _piece), res in zip(group, results):
                if ui != hold_ui:
                    if hold:
                        yield _finish(hold_ui, hold)
                    hold_ui, hold = ui, []
                hold.append(res)
        if hold:
            yield _finish(hold_ui, hold)
    finally:
        # abandon doomed work instead of waiting on it: on an error, on
        # GeneratorExit, and on shutdown
        for fut in pending.values():
            fut.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        session.close()


CKPT_EVERY = 25          # units between checkpoint flushes


def _units_sig(units, voice, speed):
    """Identity of this narration job. A checkpoint is only reusable if the
    text, voice and speed are all unchanged — re-extracting a paper or
    switching voice must start clean, not splice two narrations together."""
    h = hashlib.sha256()
    h.update(f"{voice}\x00{speed}\x00{len(units)}\x00".encode())
    for u in units:
        h.update(u["text"].encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _load_checkpoint(ckpt_path, pcm_path, sig, units):
    """Return (done, samples) to resume from, or (0, 0) to start clean.

    The PCM sidecar is truncated to exactly the samples the checkpoint
    vouches for: a crash mid-write can leave a partial tail, and splicing
    that into the output would desynchronise every later word timing."""
    try:
        ck = json.loads(ckpt_path.read_text())
    except (OSError, ValueError):
        return 0, 0
    if ck.get("sig") != sig or not pcm_path.exists():
        return 0, 0
    done, samples = int(ck.get("done", 0)), int(ck.get("samples", 0))
    if not 0 < done <= len(units):
        return 0, 0
    need = samples * 2                       # s16le mono
    if pcm_path.stat().st_size < need:       # sidecar lost data: distrust it
        return 0, 0
    if pcm_path.stat().st_size > need:
        with open(pcm_path, "r+b") as fh:
            fh.truncate(need)
    for u, saved in zip(units, ck.get("units", [])):   # restore timings
        u.update(saved)
    return done, samples


def _save_checkpoint(ckpt_path, sig, done, samples, units, pcm):
    """Flush audio first, then the checkpoint that vouches for it — never the
    reverse, or a crash between the two would claim samples that aren't on
    disk."""
    pcm.flush()
    os.fsync(pcm.fileno())
    payload = {"sig": sig, "done": done, "samples": samples,
               "units": [{k: u[k] for k in ("t0", "t1", "words") if k in u}
                         for u in units[:done]]}
    tmp = ckpt_path.with_suffix(ckpt_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, ckpt_path)               # atomic: never a half-written ckpt


def synthesize(units, out_path, voice, speed, tags=None, progress=None,
               bitrate="48k", tts_cfg=None):
    """Synthesize units, streaming raw PCM into ffmpeg as it is produced —
    encoding overlaps synthesis, only one chunk is ever in RAM, and no
    temp WAV touches disk. Writes to <out>.part and renames on success so a
    crash never leaves a truncated file at the final path. Records per-unit
    start/end times and per-word timestamps.
    progress(i, n, text) is called per unit when given; otherwise prints.

    tts_cfg["backend"]: "local" (default; on-device Kokoro) or "modal"
    (user's own Modal deployment — see modal_app.py)."""
    import numpy as np

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (needed for audio encoding)")
    tts_cfg = tts_cfg or {}
    modal_backend = tts_cfg.get("backend", "local") == "modal"

    # Resume: a long paper is thousands of TTS requests over hours, and any
    # failure used to discard every completed unit. Audio is mirrored to a raw
    # PCM sidecar as it is produced; on restart the finished units are replayed
    # from disk into a fresh encoder — no re-synthesis, no GPU spend — and
    # narration continues from the first unit that never completed.
    pcm_path = out_path.with_suffix(out_path.suffix + ".pcm")
    ckpt_path = out_path.with_suffix(out_path.suffix + ".ckpt")
    sig = _units_sig(units, voice, speed)
    done, samples = _load_checkpoint(ckpt_path, pcm_path, sig, units)
    if done:
        print(f"  resuming after {done}/{len(units)} units "
              f"({samples / SAMPLE_RATE / 60:.1f} min already narrated)",
              flush=True)

    todo = units[done:]
    producer = (_modal_unit_audio(todo, voice, speed, tts_cfg)
                if modal_backend else _local_unit_audio(todo, voice, speed))

    # m4a for the read-along view: MP4's sample table makes browser seeks
    # sample-accurate, unlike (VBR) MP3 which drifts on every seek
    codec = (["-codec:a", "aac", "-b:a", bitrate, "-movflags", "+faststart"]
             if out_path.suffix in (".m4a", ".mp4")
             else ["-codec:a", "libmp3lame", "-q:a", "3"])
    part = out_path.with_suffix(out_path.suffix + ".part")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
           *codec]
    for key, value in (tags or {}).items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-f", "mp4" if out_path.suffix in (".m4a", ".mp4") else "mp3",
            str(part)]

    # detach the encoder from terminal signals: Ctrl+C must not kill
    # ffmpeg mid-paper — the interrupted paper should resume on restart
    detach = (dict(start_new_session=True) if os.name == "posix"
              else dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, **detach)
    pcm_file = open(pcm_path, "r+b" if done else "wb")
    if done:                       # replay finished audio into the new encoder
        pcm_file.seek(0)
        left = samples * 2
        while left > 0:
            block = pcm_file.read(min(1 << 22, left))
            if not block:
                raise RuntimeError("checkpoint PCM shorter than recorded")
            proc.stdin.write(block)
            left -= len(block)
        pcm_file.seek(samples * 2)

    def push(wave):
        nonlocal samples
        pcm = (np.clip(wave, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        proc.stdin.write(pcm)
        pcm_file.write(pcm)        # the sidecar is what makes resume possible
        samples += len(wave)

    try:
        for i, (unit, chunks) in enumerate(producer, done + 1):
            text = unit["text"]
            if progress:
                progress(i, len(units), text)
            else:
                print(f"  [{i}/{len(units)}] {text[:60]}...", flush=True)
            unit["t0"] = samples / SAMPLE_RATE
            words = []
            for wave, rel_words in chunks:
                chunk_t0 = samples / SAMPLE_RATE
                words += [{"w": w["w"], "t0": round(chunk_t0 + w["t0"], 3),
                           "t1": round(chunk_t0 + w["t1"], 3)}
                          for w in rel_words]
                push(wave)
            unit["t1"] = samples / SAMPLE_RATE
            unit["words"] = words
            n_pause = int(unit["pause"] * SAMPLE_RATE)
            silence = b"\x00\x00" * n_pause
            proc.stdin.write(silence)
            pcm_file.write(silence)
            samples += n_pause
            # checkpoint on a unit boundary, where audio and timings agree
            if i % CKPT_EVERY == 0:
                _save_checkpoint(ckpt_path, sig, i, samples, units, pcm_file)
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg encode failed (rc={proc.returncode})")
        os.replace(part, out_path)
        pcm_file.close()
        # the paper is encoded: the resume scaffolding is now just disk
        pcm_path.unlink(missing_ok=True)
        ckpt_path.unlink(missing_ok=True)
        if not modal_backend:
            with TTS_LOCK:  # a long paper isn't "idle time" for the model
                _PIPE_STATE["last_used"] = time.time()
    except BrokenPipeError:
        proc.wait()
        raise RuntimeError(f"ffmpeg died during encode (rc={proc.returncode})")
    finally:
        # close the producer here rather than whenever the traceback is
        # dropped: for the remote backend that cancels in-flight batches at a
        # known point on a known thread instead of stalling the worker
        close = getattr(producer, "close", None)
        if close:
            close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if not pcm_file.closed:
            pcm_file.close()
        # The .part is a half-written container and worthless, but the PCM
        # sidecar and checkpoint are exactly what a retry resumes from — the
        # old code deleted the completed audio here, which is why a single
        # transient error cost an entire paper.
        if part.exists():
            part.unlink(missing_ok=True)
    return samples / SAMPLE_RATE


# ---------------------------------------------------------------- read-along

def build_manifest(pdf_path, units, meta, title, artist, duration):
    doc = fitz.open(pdf_path)
    pages, words_layer = [], []
    for page in doc:
        pages.append({"n": page.number, "png": f"page-{page.number:03d}.png",
                      "w": round(page.rect.width, 2), "h": round(page.rect.height, 2)})
        words_layer.append([[round(w[0], 1), round(w[1], 1), round(w[2], 1),
                             round(w[3], 1), w[4]] for w in page.get_text("words")])
    manifest_units = []
    for unit in units:
        manifest_units.append({
            "kind": unit["kind"], "text": unit["text"],
            "t0": round(unit["t0"], 3), "t1": round(unit["t1"], 3),
            "rects": unit["rects"], "words": unit["words"],
        })
    sections = [{"title": u["text"], "t0": u["t0"]}
                for u in manifest_units if u["kind"] == "heading"]
    return {"title": title, "artist": artist, "source": pdf_path.name,
            "audio": "narration.m4a", "duration": round(duration, 3),
            "pages": pages, "sections": sections, "units": manifest_units,
            "textLayer": words_layer}


def render_pages(pdf_path, out_dir, dpi):
    doc = fitz.open(pdf_path)
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        pix.save(out_dir / f"page-{page.number:03d}.png")
    return len(doc)


def write_viewer(out_dir, manifest):
    template = Path(__file__).resolve().parent / "viewer.html"
    data = json.dumps(manifest, ensure_ascii=False)
    if template.is_file():
        html = template.read_text(encoding="utf-8")
        html = html.replace("/*__PAPER_DATA__*/null", data)
        (out_dir / "index.html").write_text(html, encoding="utf-8")
    else:
        print("warning: viewer.html template missing; wrote manifest only")
    (out_dir / "manifest.json").write_text(data, encoding="utf-8")


def generate_readalong(pdf_path, out_dir, voice, speed, dpi, progress=None,
                       grobid_cfg=None, tts_cfg=None, llm_cfg=None,
                       prepared=None):
    """Full pipeline: PDF -> readalong bundle in out_dir. Returns summary
    dict. progress(fraction, label) covers the whole pipeline: synthesis
    (with encoding overlapped) maps to 0-0.95, then pages/manifest — so the
    bar doesn't sit at a false 100% during the post-synthesis stages."""
    def unit_cb(i, n, text):
        progress(0.95 * i / n, "narrating")

    # Extraction reports nothing and can run for minutes (a remote model may
    # be cold-starting), so a bare 0 % is indistinguishable from a hang. Name
    # the stage before it begins. `prepared` lets a caller supply an extraction
    # it already ran (the server overlaps it with the previous paper's audio).
    if prepared is not None:
        units, meta, warnings = prepared
    else:
        if progress:
            progress(0.0, "extracting text")
        units, meta, warnings = prepare_units(pdf_path, grobid_cfg, llm_cfg)
    tags = make_tags(pdf_path, meta)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "narration.mp3").unlink(missing_ok=True)  # pre-m4a leftover
    tts_cfg = tts_cfg or {}
    duration = synthesize(units, out_dir / "narration.m4a", voice, speed,
                          tags, unit_cb if progress else None,
                          tts_cfg.get("m4a_bitrate", "48k"), tts_cfg)
    if progress:
        progress(0.96, "rendering pages")
    render_pages(pdf_path, out_dir, dpi)
    if progress:
        progress(0.98, "building manifest")
    manifest = build_manifest(pdf_path, units, meta, tags["title"],
                              tags["artist"], duration)
    write_viewer(out_dir, manifest)
    if progress:
        progress(1.0, "done")
    return {"title": tags["title"], "authors": meta["authors"],
            "year": meta["year"], "duration": duration,
            "units": len(units), "warnings": warnings}


# ---------------------------------------------------------------------- main

def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, nargs="?", help="input paper PDF")
    parser.add_argument("-o", "--output", type=Path,
                        help="output MP3 (default: next to the PDF)")
    parser.add_argument("--voice", default=cfg["tts"]["voice"],
                        help="Kokoro voice id")
    parser.add_argument("--speed", type=float, default=cfg["tts"]["speed"],
                        help="speech speed")
    parser.add_argument("--dpi", type=int, default=cfg["render"]["dpi"],
                        help="read-along page render DPI")
    parser.add_argument("--text-only", action="store_true",
                        help="print the cleaned text instead of synthesizing")
    parser.add_argument("--readalong", action="store_true",
                        help="(re)generate the <paper>.readalong/ browser view")
    parser.add_argument("--play", action="store_true",
                        help="open the read-along view, generating it if missing")
    parser.add_argument("--gui", action="store_true",
                        help="start the library web app")
    parser.add_argument("--library", type=Path,
                        default=library_path(cfg),
                        help="library folder for --gui")
    parser.add_argument("--port", type=int, default=cfg["gui"]["port"],
                        help="port for --gui")
    parser.add_argument("--no-open", action="store_true",
                        help="with --gui: don't open a browser")
    parser.add_argument("--no-grobid", action="store_true",
                        help="skip GROBID; use the built-in extractor")
    parser.add_argument("--llm", action="store_true",
                        help="use the LLM block-classification extractor on "
                             "this run (as if [llm] enabled)")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the LLM extractor even if [llm] is enabled")
    args = parser.parse_args()
    grobid_cfg = None if args.no_grobid else cfg["grobid"]
    llm_cfg = None if args.no_llm else dict(cfg["llm"])
    if args.llm and llm_cfg is not None:
        llm_cfg["enabled"] = True

    if args.gui:
        import server
        server.run(args.library, args.port, voice=args.voice,
                   speed=args.speed, dpi=args.dpi,
                   open_browser=cfg["gui"]["open"] and not args.no_open,
                   grobid_cfg=grobid_cfg, tts_cfg=cfg["tts"],
                   idle_exit_min=cfg["gui"].get("idle_exit_min", 0),
                   llm_cfg=llm_cfg, auth_cfg=cfg.get("auth"))
        return

    if args.pdf is None:
        parser.error("a PDF is required (or use --gui)")
    if not args.pdf.is_file():
        sys.exit(f"error: no such file: {args.pdf}")

    if args.text_only:
        try:
            units, _, warnings = prepare_units(args.pdf, grobid_cfg, llm_cfg)
        except ValueError as e:
            sys.exit(f"error: {e}")
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        paragraph = []
        for u in units:
            if u["kind"] == "heading":
                if paragraph:
                    print("\n" + " ".join(paragraph))
                    paragraph = []
                print(f"\n## {u['text']}")
            else:
                paragraph.append(u["text"])
                if u["para_end"]:
                    print("\n" + " ".join(paragraph))
                    paragraph = []
        if paragraph:
            print("\n" + " ".join(paragraph))
        return

    if args.readalong or args.play:
        out_dir = args.pdf.with_suffix(".readalong")
        index = out_dir / "index.html"
        if args.readalong or not index.is_file():
            try:
                info = generate_readalong(args.pdf, out_dir, args.voice,
                                          args.speed, args.dpi,
                                          grobid_cfg=grobid_cfg,
                                          tts_cfg=cfg["tts"], llm_cfg=llm_cfg)
            except ValueError as e:
                sys.exit(f"error: {e}")
            for w in info["warnings"]:
                print(f"warning: {w}")
            print(f"read-along view: {out_dir}")
        if args.play:
            import webbrowser
            webbrowser.open(index.as_uri())
            print(f"opened {index}")
        return

    try:
        units, meta, warnings = prepare_units(args.pdf, grobid_cfg, llm_cfg)
    except ValueError as e:
        sys.exit(f"error: {e}")
    for w in warnings:
        print(f"warning: {w}")
    words = sum(len(u["text"].split()) for u in units)
    print(f"{len(units)} units, ~{words} words (~{words / 170:.0f} min of audio)")
    out_path = args.output or args.pdf.with_suffix(".mp3")
    duration = synthesize(units, out_path, args.voice, args.speed,
                          make_tags(args.pdf, meta), tts_cfg=cfg["tts"])
    print(f"done: {out_path}  ({duration / 60:.1f} min)")


if __name__ == "__main__":
    main()
