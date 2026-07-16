# Rhapsode

**R**ead-along **H**ighlighted **A**udio for **P**aper**S** **O**n-**D**evice **E**ngine —
turn academic paper PDFs into narrated, synchronized read-along audio, fully
locally. A *rhapsode* was the ancient Greek performer who recited scholarly
texts aloud; this one runs on your GPU.

Rhapsode extracts a paper's actual reading content — title, abstract, and body
sections in correct two-column order, minus author blocks, captions, page
furniture, citation brackets, and references — and narrates it with
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). Because Kokoro
reports when each word is spoken, the browser viewer highlights the exact
sentence on the real PDF page as you listen.

## Sixty-second start

```bash
git clone https://github.com/saimlau/rhapsode && cd rhapsode
python3 -m venv .venv
.venv/bin/pip install pymupdf kokoro soundfile requests fastapi uvicorn python-multipart
./rhapsode "some-paper.pdf" --play
```

The first run downloads the ~330 MB Kokoro model; after that everything is
offline. `--play` opens the read-along view in your browser. See
[Installation](installation.md) for prerequisites (`ffmpeg`, `espeak-ng`) and
platform notes.

## What you get

- **CLI** — `./rhapsode paper.pdf` writes a tagged MP3 next to the PDF;
  `--play` builds a self-contained browser read-along view.
  → [CLI & read-along viewer](usage.md)
- **Library GUI** — `--gui` starts a local web app: drag-drop PDFs, a
  generation queue, playlists, and podcast-style auto-advance with remembered
  positions. → [Library GUI](library.md)
- **Zotero plugin** — right-click any item or collection in Zotero and listen
  inside a Zotero tab; collections become playlists.
  → [Zotero plugin](zotero.md)
- **Structure-aware extraction** — [GROBID](https://github.com/kermitt2/grobid)
  as the primary extractor with tuned built-in heuristics as the offline
  fallback. → [Installation § GROBID](installation.md#grobid-primary-extractor)
- **Runs where you want** — local CUDA / Apple-Silicon MPS / CPU, or serverless
  on your own Modal account. → [Compute backends](backends.md)
- **System voice** — Kokoro as a Speech Dispatcher voice for any
  speechd-aware Linux app. → [System voice](system-voice.md)

Everything is configured in one `config.toml` —
see the [configuration reference](configuration.md). Stuck? Try
[Troubleshooting & FAQ](troubleshooting.md).

!!! note "Platform support"
    Linux is first-class (developed and tested there). macOS and Windows are
    expected to work but experimental — testers welcome. Details in
    [Installation](installation.md#platform-notes).
