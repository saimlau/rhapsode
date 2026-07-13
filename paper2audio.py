#!/usr/bin/env python3
"""Convert a two-column academic paper PDF into a narrated MP3.

Extracts title, abstract, and body text in reading order (PyMuPDF),
skips affiliations / page furniture / figure captions / References,
strips citation brackets, and synthesizes speech locally with Kokoro
(CUDA when available). `--play` builds and opens a browser read-along
view with synced sentence highlighting. See docs/superpowers/specs/.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

from extraction import (MappedText, clean_mapped, clean_text,
                        extract_segments, merge_continuations, split_sentences)

SAMPLE_RATE = 24000
HEADING_PAUSE_S = 0.7
PARAGRAPH_PAUSE_S = 0.35
SENTENCE_PAUSE_S = 0.08
RENDER_DPI = 150


def build_units(segments):
    """Flatten segments into per-sentence synthesis units."""
    units = []
    for kind, mt in segments:
        cleaned = clean_mapped(mt)
        if not cleaned.text:
            continue
        if kind == "heading":
            units.append({"kind": "heading", "mt": cleaned,
                          "pause": HEADING_PAUSE_S})
        else:
            sentences = split_sentences(cleaned)
            for j, sentence in enumerate(sentences):
                last = j == len(sentences) - 1
                units.append({"kind": "body", "mt": sentence,
                              "pause": PARAGRAPH_PAUSE_S if last else SENTENCE_PAUSE_S})
    return units


# ----------------------------------------------------------------- synthesis

def synthesize(units, out_path, voice, speed, tags=None):
    """Synthesize units to MP3, recording per-unit start/end times and
    (when Kokoro provides them) per-word timestamps."""
    import numpy as np
    import soundfile as sf
    import torch
    from kokoro import KPipeline

    if not shutil.which("ffmpeg"):
        sys.exit("error: ffmpeg not found on PATH (needed for MP3 encoding)")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("warning: CUDA not available, synthesizing on CPU (slower)")
    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=device)

    parts, samples = [], 0
    for i, unit in enumerate(units, 1):
        text = unit["mt"].text
        print(f"  [{i}/{len(units)}] {text[:60]}...", flush=True)
        unit["t0"] = samples / SAMPLE_RATE
        words = []
        for item in pipeline(text, voice=voice, speed=speed):
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

    wave = np.concatenate(parts)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        sf.write(tmp.name, wave, SAMPLE_RATE, subtype="PCM_16")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name,
               "-codec:a", "libmp3lame", "-q:a", "3"]
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
            "kind": unit["kind"], "text": unit["mt"].text,
            "t0": round(unit["t0"], 3), "t1": round(unit["t1"], 3),
            "rects": unit["mt"].rects(), "words": unit["words"],
        })
    sections = [{"title": u["text"], "t0": u["t0"]}
                for u in manifest_units if u["kind"] == "heading"]
    return {"title": title, "artist": artist, "source": pdf_path.name,
            "audio": "narration.mp3", "duration": round(duration, 3),
            "pages": pages, "sections": sections, "units": manifest_units,
            "textLayer": words_layer}


def render_pages(pdf_path, out_dir):
    doc = fitz.open(pdf_path)
    for page in doc:
        pix = page.get_pixmap(dpi=RENDER_DPI)
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


# ---------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path, help="input paper PDF")
    parser.add_argument("-o", "--output", type=Path,
                        help="output MP3 (default: next to the PDF)")
    parser.add_argument("--voice", default="af_heart", help="Kokoro voice id")
    parser.add_argument("--speed", type=float, default=1.0, help="speech speed")
    parser.add_argument("--text-only", action="store_true",
                        help="print the cleaned text instead of synthesizing")
    parser.add_argument("--readalong", action="store_true",
                        help="(re)generate the <paper>.readalong/ browser view")
    parser.add_argument("--play", action="store_true",
                        help="open the read-along view, generating it if missing")
    args = parser.parse_args()

    if not args.pdf.is_file():
        sys.exit(f"error: no such file: {args.pdf}")

    segments, found_references, meta = extract_segments(args.pdf)
    segments = merge_continuations(segments)
    while segments and segments[-1][0] == "heading":
        segments.pop()  # orphan trailing heading (e.g. Declarations with small-font body)

    total_chars = sum(len(mt) for _, mt in segments)
    if total_chars < 500:
        sys.exit("error: almost no text extracted — is this a scanned/image-only PDF?")
    if not found_references:
        print("warning: no References heading found; reading to the end of the PDF")

    if args.text_only:
        for kind, mt in segments:
            text = clean_mapped(mt).text
            if text:
                print(f"\n## {text}" if kind == "heading" else f"\n{text}")
        return

    title = clean_text(meta["title"] or "") or args.pdf.stem
    artist = (f"{meta['authors']} (audio by paper2audio)" if meta["authors"]
              else "audio by paper2audio")
    tags = {"title": title, "artist": artist}

    units = build_units(segments)
    words = sum(len(u["mt"].text.split()) for u in units)
    print(f"{len(units)} units, ~{words} words (~{words / 170:.0f} min of audio)")

    if args.readalong or args.play:
        out_dir = args.pdf.with_suffix(".readalong")
        index = out_dir / "index.html"
        if args.readalong or not index.is_file():
            out_dir.mkdir(exist_ok=True)
            duration = synthesize(units, out_dir / "narration.mp3",
                                  args.voice, args.speed, tags)
            render_pages(args.pdf, out_dir)
            manifest = build_manifest(args.pdf, units, meta, title, artist, duration)
            write_viewer(out_dir, manifest)
            print(f"read-along view: {out_dir}")
        if args.play:
            subprocess.Popen(["xdg-open", str(index)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"opened {index}")
        return

    out_path = args.output or args.pdf.with_suffix(".mp3")
    duration = synthesize(units, out_path, args.voice, args.speed, tags)
    print(f"done: {out_path}  ({duration / 60:.1f} min)")


if __name__ == "__main__":
    main()
