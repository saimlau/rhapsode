# paper2audio

Turn a two-column academic paper PDF into a narrated MP3, fully locally.
Extraction with PyMuPDF, speech with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
on CUDA (falls back to CPU).

## Usage

```bash
./paper2audio "paper.pdf"                 # writes paper.mp3 next to the PDF
./paper2audio "paper.pdf" -o out.mp3      # explicit output path
./paper2audio "paper.pdf" --text-only    # inspect what will be read
./paper2audio "paper.pdf" --voice af_heart --speed 1.1
```

What gets read: title, abstract, keywords, and body sections, in
two-column reading order. What gets skipped: author list, affiliations,
page headers/footers, figure/table captions, citation brackets like
`[12]`, and everything from the References heading onward.

## Setup (already done on this machine)

```bash
python3 -m venv .venv
.venv/bin/pip install pymupdf kokoro soundfile
```

Requires `ffmpeg` and `espeak-ng` on the system. First run downloads the
~330 MB Kokoro model from Hugging Face; after that it is fully offline.

## Notes

- Heuristics (font-size thresholds, header/footer zones) were tuned on
  Springer layouts but are ratio-based, so other publishers should mostly
  work — always spot-check a new layout with `--text-only` first.
- Scanned/image-only PDFs are rejected (no OCR).
- Design spec: `docs/superpowers/specs/2026-07-12-paper2audio-design.md`.
