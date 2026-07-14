#!/usr/bin/env python3
"""Convert a two-column academic paper PDF into a narrated MP3.

Extracts title, abstract, and body text in reading order (PyMuPDF),
skips affiliations / page furniture / figure captions / References,
strips citation brackets, and synthesizes speech locally with Kokoro
(CUDA when available). `--play` builds and opens a browser read-along
view with synced sentence highlighting; `--gui` starts the library web
app. See docs/superpowers/specs/.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import fitz  # PyMuPDF

from config import load_config, library_path
from extraction import (MappedText, clean_mapped, clean_text,
                        extract_segments, merge_continuations, split_sentences)

SAMPLE_RATE = 24000
HEADING_PAUSE_S = 0.7
PARAGRAPH_PAUSE_S = 0.35
SENTENCE_PAUSE_S = 0.08

_PIPELINE = None  # warm Kokoro model, reused across papers in one process
TTS_LOCK = threading.Lock()  # one inference at a time (worker + /tts endpoint)


def prepare_units(pdf_path, grobid_cfg=None):
    """Extract and clean a paper. Returns (units, meta, warnings); units are
    {kind, text, rects, para_end, pause}. GROBID is the primary backend when
    configured; the built-in heuristics are the fallback.

    Raises ValueError for PDFs with no usable text (scanned/image-only).
    """
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
    artist = (f"{meta['authors']} (audio by paper2audio)" if meta["authors"]
              else "audio by paper2audio")
    return {"title": title, "artist": artist}


# ----------------------------------------------------------------- synthesis

def get_pipeline():
    """Load Kokoro once per process; queued papers reuse the warm model."""
    global _PIPELINE
    if _PIPELINE is None:
        import torch
        from kokoro import KPipeline
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            print("warning: CUDA not available, synthesizing on CPU (slower)")
        _PIPELINE = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M",
                              device=device)
    return _PIPELINE


def synthesize(units, out_path, voice, speed, tags=None, progress=None):
    """Synthesize units to MP3/M4A, recording per-unit start/end times and
    (when Kokoro provides them) per-word timestamps. progress(i, n, text)
    is called per unit when given; otherwise progress prints to stdout."""
    import numpy as np
    import soundfile as sf

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH (needed for audio encoding)")
    pipeline = get_pipeline()

    parts, samples = [], 0
    for i, unit in enumerate(units, 1):
        text = unit["text"]
        if progress:
            progress(i, len(units), text)
        else:
            print(f"  [{i}/{len(units)}] {text[:60]}...", flush=True)
        unit["t0"] = samples / SAMPLE_RATE
        words = []
        with TTS_LOCK:
            results = list(pipeline(text, voice=voice, speed=speed))
        for item in results:
            audio = getattr(item, "audio", None)
            if audio is None:
                _, _, audio = item
            chunk_t0 = samples / SAMPLE_RATE
            for tok in getattr(item, "tokens", None) or []:
                if getattr(tok, "start_ts", None) is not None:
                    words.append({"w": tok.text,
                                  "t0": round(chunk_t0 + tok.start_ts, 3),
                                  "t1": round(chunk_t0 + tok.end_ts, 3)})
            wave = audio.detach().cpu().numpy()
            parts.append(wave)
            samples += len(wave)
        unit["t1"] = samples / SAMPLE_RATE
        unit["words"] = words
        silence = np.zeros(int(unit["pause"] * SAMPLE_RATE), dtype=np.float32)
        parts.append(silence)
        samples += len(silence)

    if progress:
        progress(len(units), len(units), "encoding audio")
    wave = np.concatenate(parts)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        sf.write(tmp.name, wave, SAMPLE_RATE, subtype="PCM_16")
        # m4a for the read-along view: MP4's sample table makes browser seeks
        # sample-accurate, unlike (VBR) MP3 which drifts on every seek
        codec = (["-codec:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]
                 if out_path.suffix in (".m4a", ".mp4")
                 else ["-codec:a", "libmp3lame", "-q:a", "3"])
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name] + codec
        for key, value in (tags or {}).items():
            if value:
                cmd += ["-metadata", f"{key}={value}"]
        subprocess.run(cmd + [str(out_path)], check=True)
    return len(wave) / SAMPLE_RATE


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
                       grobid_cfg=None):
    """Full pipeline: PDF -> readalong bundle in out_dir. Returns summary
    dict. progress(fraction, label) covers the whole pipeline: synthesis
    maps to 0-0.87, then encode/pages/manifest — so the bar doesn't sit at
    a false 100% during the post-synthesis stages."""
    def unit_cb(i, n, text):
        progress(0.87 * i / n, text)

    units, meta, warnings = prepare_units(pdf_path, grobid_cfg)
    tags = make_tags(pdf_path, meta)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "narration.mp3").unlink(missing_ok=True)  # pre-m4a leftover
    duration = synthesize(units, out_dir / "narration.m4a", voice, speed,
                          tags, unit_cb if progress else None)
    if progress:
        progress(0.92, "rendering pages")
    render_pages(pdf_path, out_dir, dpi)
    if progress:
        progress(0.97, "building manifest")
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
    args = parser.parse_args()
    grobid_cfg = None if args.no_grobid else cfg["grobid"]

    if args.gui:
        import server
        server.run(args.library, args.port, voice=args.voice,
                   speed=args.speed, dpi=args.dpi,
                   open_browser=cfg["gui"]["open"] and not args.no_open,
                   grobid_cfg=grobid_cfg)
        return

    if args.pdf is None:
        parser.error("a PDF is required (or use --gui)")
    if not args.pdf.is_file():
        sys.exit(f"error: no such file: {args.pdf}")

    if args.text_only:
        try:
            units, _, warnings = prepare_units(args.pdf, grobid_cfg)
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
                                          grobid_cfg=grobid_cfg)
            except ValueError as e:
                sys.exit(f"error: {e}")
            for w in info["warnings"]:
                print(f"warning: {w}")
            print(f"read-along view: {out_dir}")
        if args.play:
            subprocess.Popen(["xdg-open", str(index)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"opened {index}")
        return

    try:
        units, meta, warnings = prepare_units(args.pdf, grobid_cfg)
    except ValueError as e:
        sys.exit(f"error: {e}")
    for w in warnings:
        print(f"warning: {w}")
    words = sum(len(u["text"].split()) for u in units)
    print(f"{len(units)} units, ~{words} words (~{words / 170:.0f} min of audio)")
    out_path = args.output or args.pdf.with_suffix(".mp3")
    duration = synthesize(units, out_path, args.voice, args.speed,
                          make_tags(args.pdf, meta))
    print(f"done: {out_path}  ({duration / 60:.1f} min)")


if __name__ == "__main__":
    main()
