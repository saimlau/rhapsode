# Rhapsode

**R**ead-along **H**ighlighted **A**udio for **P**aper**S** **O**n-**D**evice **E**ngine —
turn academic paper PDFs into narrated, synchronized read-along audio,
fully locally. (A *rhapsode* was the ancient Greek performer who recited
scholarly texts aloud.)
Extraction with PyMuPDF, speech with [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
on CUDA (falls back to CPU).

## Usage

```bash
./rhapsode "paper.pdf"                 # writes paper.mp3 next to the PDF
./rhapsode "paper.pdf" --play          # browser read-along view (see below)
./rhapsode --gui                       # library web app (see below)
./rhapsode "paper.pdf" -o out.mp3      # explicit output path
./rhapsode "paper.pdf" --text-only    # inspect what will be read
./rhapsode "paper.pdf" --voice af_heart --speed 1.1
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

Papers can be organized into **playlists** (a paper may be in several;
audio exists once): the sidebar header switches between All papers and
named playlists, ＋ on a paper row adds it to one, and within a playlist
✕ removes it from the playlist only. Reorder and auto-advance follow the
active playlist.

## Zotero plugin

`zotero-plugin/` adds "Listen with Rhapsode" to Zotero's item context
menu (PDF → local server, read-along in a Zotero tab, server auto-started)
and "Listen to collection with Rhapsode" to the collection menu: every
PDF in the collection becomes part of a playlist named after it, with
subcollections as their own "Parent / Child" playlists. Install
for development with `zotero-plugin/dev-install.sh` while Zotero is
closed, then restart Zotero. Requires Zotero 7/8.

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
list plus "(audio by Rhapsode)" (`artist`), so it shows up properly
in podcast/music players.

## Setup (already done on this machine)

```bash
python3 -m venv .venv
.venv/bin/pip install pymupdf kokoro soundfile
```

Requires `ffmpeg` and `espeak-ng` on the system. First run downloads the
~330 MB Kokoro model from Hugging Face; after that it is fully offline.

## GROBID (primary extractor)

Structure extraction uses [GROBID](https://github.com/kermitt2/grobid)
when available — the standard ML pipeline for scholarly PDFs — with the
built-in heuristics as offline fallback (`--no-grobid` forces them).
Native install expected at `[grobid] home` (see config): GROBID source
+ `jdk/` subdir with JDK 17, built once with
`JAVA_HOME=$PWD/jdk ./gradlew clean install -x test`. The service is
auto-started on demand and gives per-sentence coordinates, so read-along
highlighting works identically on both backends.

## Notes

- Heuristics (font-size thresholds, header/footer zones) were tuned on
  Springer layouts but are ratio-based, so other publishers should mostly
  work — always spot-check a new layout with `--text-only` first.
- Scanned/image-only PDFs are rejected (no OCR).
- Design spec: `docs/superpowers/specs/2026-07-12-Rhapsode-design.md`.

## Compute backends

Synthesis runs locally by default (GPU strongly recommended, CPU works).
No GPU? Deploy `modal_app.py` to your own [Modal](https://modal.com)
account (`pip install modal && modal setup && modal deploy modal_app.py`)
and set `[tts] backend = "modal"` + `modal_endpoint` in config.toml —
inference then runs serverless on your Modal credits (free tier covers
hundreds of audio-hours/month) while extraction, encoding, and the
viewer stay on your machine. Word-level timing is identical on both
backends (same Kokoro code).

## Kokoro as a system voice (Speech Dispatcher)

`speechd/install-speechd-voice.sh` registers Kokoro as a user-level
Speech Dispatcher voice (no root): it appears in Zotero's built-in
read-aloud voice list and any other speechd-aware app. Requires the
Rhapsode server to be running; test with
`spd-say -o kokoro 'Hello from Kokoro'`.
