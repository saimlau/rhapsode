#!/usr/bin/env python3
"""Convert a two-column academic paper PDF into a narrated MP3.

Extracts title, abstract, and body text in reading order (PyMuPDF),
skips affiliations / page furniture / figure captions / References,
strips citation brackets, and synthesizes speech locally with Kokoro
(CUDA when available). See docs/superpowers/specs/ for the design.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF

SAMPLE_RATE = 24000
CHUNK_CHAR_LIMIT = 450
HEADING_PAUSE_S = 0.7
CHUNK_PAUSE_S = 0.15
STOP_HEADINGS = re.compile(r"^(references|bibliography|literature\s+cited)\b", re.I)
CITATION_RE = re.compile(r"\s*\[[0-9,;\s–—-]+\]")


# ---------------------------------------------------------------- extraction

def _block_text_and_size(block):
    """Return (text, dominant_font_size) for a text block."""
    size_weight = {}
    lines = []
    for line in block["lines"]:
        parts = []
        for span in line["spans"]:
            parts.append(span["text"])
            key = round(span["size"], 1)
            size_weight[key] = size_weight.get(key, 0) + len(span["text"])
        lines.append("".join(parts))
    text = "\n".join(lines)
    size = max(size_weight, key=size_weight.get) if size_weight else 0.0
    return text, size


def _body_font_size(doc):
    """Dominant span size by character count across the document."""
    weights = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    key = round(span["size"], 1)
                    weights[key] = weights.get(key, 0) + len(span["text"])
    if not weights:
        return 0.0
    return max(weights, key=weights.get)


def _order_blocks(blocks, page_width):
    """Reading order for a (possibly) two-column page.

    Blocks spanning the midline act as flow breaks; between them, the
    left column is read top-to-bottom, then the right column.
    """
    mid = page_width / 2
    ordered, left, right = [], [], []

    def flush():
        ordered.extend(sorted(left, key=lambda b: b["bbox"][1]))
        ordered.extend(sorted(right, key=lambda b: b["bbox"][1]))
        left.clear()
        right.clear()

    for block in sorted(blocks, key=lambda b: b["bbox"][1]):
        x0, _, x1, _ = block["bbox"]
        if x0 < mid - 20 and x1 > mid + 20:
            flush()
            ordered.append(block)
        elif (x0 + x1) / 2 < mid:
            left.append(block)
        else:
            right.append(block)
    flush()
    return ordered


def _is_author_list(text):
    """Springer-style author line: names joined by '·' with affiliation digits."""
    return text.count("·") >= 2 and re.search(r"\d(?:,\d+)*\s*·", text)


def extract_segments(pdf_path):
    """Return a list of ('heading'|'body', text) segments in reading order."""
    doc = fitz.open(pdf_path)
    body_size = _body_font_size(doc)
    segments = []

    for page_no, page in enumerate(doc):
        height = page.rect.height
        blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
        for block in _order_blocks(blocks, page.rect.width):
            _, y0, _, y1 = block["bbox"]
            if y1 < 0.07 * height or y0 > 0.92 * height:
                continue  # running header / footer zone
            text, size = _block_text_and_size(block)
            flat = " ".join(text.split())
            if not flat:
                continue
            if size < body_size - 0.6:
                continue  # captions, affiliations, received/copyright lines
            if page_no == 0 and _is_author_list(flat):
                continue
            is_heading = size >= body_size + 0.8 and len(flat) < 120
            if is_heading and STOP_HEADINGS.match(flat):
                return segments, True
            if is_heading:
                segments.append(("heading", flat))
            elif page_no == 0 and flat.startswith("Abstract"):
                segments.append(("heading", "Abstract."))
                segments.append(("body", re.sub(r"^\s*Abstract\s*", "", text)))
            elif page_no == 0 and flat.startswith("Keywords"):
                keywords = flat[len("Keywords"):].lstrip().replace(" · ", ", ")
                segments.append(("body", "Keywords: " + keywords + "."))
            else:
                segments.append(("body", text))
    return segments, False


# ------------------------------------------------------------------ cleaning

def clean_text(text):
    text = re.sub(r"[‐‑‒­]", "-", text)  # unicode hyphens
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate line breaks
    text = re.sub(r"\s*\n\s*", " ", text)         # join wrapped lines
    text = CITATION_RE.sub("", text)              # [12], [8, 13-15]
    text = re.sub(r"(\d)\s*–\s*(\d)", r"\1 to \2", text)  # 5–10 → 5 to 10
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def sentence_chunks(text, limit=CHUNK_CHAR_LIMIT):
    """Split into sentence-aligned chunks of at most ~limit characters."""
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(“\"])", text)
    chunks, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def build_playlist(segments):
    """Flatten segments into (text, pause_after_seconds) synthesis units."""
    playlist = []
    for kind, raw in segments:
        text = clean_text(raw)
        if not text:
            continue
        if kind == "heading":
            playlist.append((text, HEADING_PAUSE_S))
        else:
            for chunk in sentence_chunks(text):
                playlist.append((chunk, CHUNK_PAUSE_S))
    return playlist


# ----------------------------------------------------------------- synthesis

def synthesize(playlist, out_path, voice, speed):
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

    parts = []
    for i, (text, pause) in enumerate(playlist, 1):
        print(f"  [{i}/{len(playlist)}] {text[:60]}...", flush=True)
        for item in pipeline(text, voice=voice, speed=speed):
            audio = getattr(item, "audio", None)
            if audio is None:
                _, _, audio = item
            parts.append(audio.detach().cpu().numpy())
        parts.append(np.zeros(int(pause * SAMPLE_RATE), dtype=np.float32))

    wave = np.concatenate(parts)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        sf.write(tmp.name, wave, SAMPLE_RATE, subtype="PCM_16")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name,
             "-codec:a", "libmp3lame", "-q:a", "3", str(out_path)],
            check=True,
        )
    return len(wave) / SAMPLE_RATE


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
    args = parser.parse_args()

    if not args.pdf.is_file():
        sys.exit(f"error: no such file: {args.pdf}")

    segments, found_references = extract_segments(args.pdf)
    while segments and segments[-1][0] == "heading":
        segments.pop()  # orphan trailing heading (e.g. Declarations with small-font body)
    total_chars = sum(len(t) for _, t in segments)
    if total_chars < 500:
        sys.exit("error: almost no text extracted — is this a scanned/image-only PDF?")
    if not found_references:
        print("warning: no References heading found; reading to the end of the PDF")

    if args.text_only:
        for kind, raw in segments:
            text = clean_text(raw)
            if not text:
                continue
            print(f"\n## {text}" if kind == "heading" else f"\n{text}")
        return

    playlist = build_playlist(segments)
    words = sum(len(t.split()) for t, _ in playlist)
    print(f"{len(playlist)} chunks, ~{words} words (~{words / 170:.0f} min of audio)")

    out_path = args.output or args.pdf.with_suffix(".mp3")
    duration = synthesize(playlist, out_path, args.voice, args.speed)
    print(f"done: {out_path}  ({duration / 60:.1f} min)")


if __name__ == "__main__":
    main()
