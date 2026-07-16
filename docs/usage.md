# CLI & read-along viewer

## Command line

```bash
./rhapsode "paper.pdf"                  # write paper.mp3 next to the PDF
./rhapsode "paper.pdf" --play           # read-along view in the browser
./rhapsode "paper.pdf" --text-only      # preview what will be read
./rhapsode --gui                        # library web app
```

| Flag | Effect |
|---|---|
| `pdf` | Input paper PDF (optional only with `--gui`). |
| `-o, --output PATH` | Output MP3 path (default: next to the PDF). |
| `--voice ID` | Kokoro voice id (default from config; see [voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)). |
| `--speed X` | Speech speed multiplier at synthesis time. |
| `--dpi N` | Page-image resolution for the read-along view. |
| `--text-only` | Print the cleaned narration text instead of synthesizing. |
| `--readalong` | (Re)generate `<paper>.readalong/` without opening it. |
| `--play` | Open the read-along view, generating it first if missing. |
| `--gui` | Start the [library web app](library.md). |
| `--library PATH` | Library folder for `--gui` (default from config). |
| `--port N` | Port for `--gui`. |
| `--no-open` | With `--gui`: don't open a browser tab. |
| `--no-grobid` | Skip GROBID; force the built-in heuristic extractor. |

Flags override `config.toml`, which overrides built-in defaults.

## What gets read

Title, abstract, keywords, and body sections, in two-column reading order.
Skipped: the author list and affiliations, page headers/footers,
figure/table captions, citation brackets like `[12]`, and everything from
the References heading onward.

!!! tip
    Extraction heuristics are ratio-based and publisher-agnostic, but
    spot-check an unfamiliar layout with `--text-only` before committing to
    a 40-minute synthesis.

The MP3 is ID3-tagged with the paper title and the author list plus
"(audio by Rhapsode)", so it files correctly in podcast/music players.

## The `.readalong` bundle

`--play`/`--readalong` build `<paper>.readalong/` next to the PDF: rendered
page images, the narration audio (`.m4a` — AAC seeks are sample-accurate in
browsers, unlike VBR MP3), a timing manifest with sentence rectangles and
per-word timestamps, and a self-contained `index.html`. The folder is fully
offline and needs no server — open `index.html` anywhere.

## Using the viewer

- The sentence being spoken is **highlighted on the actual page** and
  auto-scrolled into view. Scrolling manually pauses following; the
  "Follow narration" chip re-engages it.
- The **floating panel** (drag by the dotted grip; double-click the grip to
  collapse) has play/pause, sentence and section jumps, a section-segmented
  timeline, playback speed, and volume. Playback speed is instant — it
  doesn't re-synthesize.
- **Text is selectable** like a real PDF. Right-click a selection for
  **Copy** or **Start from here**, which jumps narration to that sentence.

Keyboard: ++space++ play/pause · ++left++ / ++right++ ±10 s ·
++comma++ / ++period++ previous/next sentence ·
++bracket-left++ / ++bracket-right++ previous/next section.
