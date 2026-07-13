# paper2audio

Turn a two-column academic paper PDF into a narrated MP3, fully locally.
Extraction with PyMuPDF, speech with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
on CUDA (falls back to CPU).

## Usage

```bash
./paper2audio "paper.pdf"                 # writes paper.mp3 next to the PDF
./paper2audio "paper.pdf" --play          # browser read-along view (see below)
./paper2audio --gui                       # library web app (see below)
./paper2audio "paper.pdf" -o out.mp3      # explicit output path
./paper2audio "paper.pdf" --text-only    # inspect what will be read
./paper2audio "paper.pdf" --voice af_heart --speed 1.1
```

## Library GUI

`--gui` starts a local web app (127.0.0.1 only) and opens the browser:
drag-drop PDFs anywhere on the page to add them, watch generation
progress live (one GPU worker, model kept warm between papers), and
listen through the queue. Papers auto-advance podcast-style (toggleable);
your position in each paper is remembered server-side. Drag cards to
reorder; hover a card for regenerate/remove.

The library (imported PDFs + generated bundles + `library.json`) lives at
the path set in `config.toml`.

## Configuration

Copy `config.example.toml` to `config.toml` (gitignored) and edit:
library path, default voice/speed, render DPI, GUI port. Precedence:
CLI flags > config.toml > built-in defaults.

## Read-along view

`--play` builds `<paper>.readalong/` next to the PDF (page images +
narration + a self-contained `index.html`) and opens it in the browser;
`--readalong` rebuilds it without opening. Fully offline, no server.

- The sentence being spoken is highlighted and auto-scrolled into view;
  scrolling manually pauses following ("Follow narration" re-engages it).
- Floating panel (drag by the dotted grip, double-click it to collapse):
  play/pause, sentence/section jumps, section-segmented timeline, speed,
  volume.
- Text is selectable like a real PDF; right-click gives **Copy** and
  **Start from here**.
- Keyboard: Space play/pause · ←/→ ±10 s · ,/. sentence · [/] section.

What gets read: title, abstract, keywords, and body sections, in
two-column reading order. What gets skipped: author list, affiliations,
page headers/footers, figure/table captions, citation brackets like
`[12]`, and everything from the References heading onward.

The MP3 is ID3-tagged with the paper title (`title`) and the author
list plus "(audio by paper2audio)" (`artist`), so it shows up properly
in podcast/music players.

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
