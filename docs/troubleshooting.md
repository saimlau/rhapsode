# Troubleshooting & FAQ

## Extraction

**"error: no extractable text"** — the PDF is scanned/image-only. Rhapsode
has no OCR; run the PDF through OCR first (e.g. `ocrmypdf`) and retry.

**Narration includes junk, or skips real content.** Check what will be read
before synthesizing:

```bash
./rhapsode "paper.pdf" --text-only
```

If GROBID mis-parses a particular paper, `--no-grobid` forces the built-in
heuristic extractor for that run — the two make different mistakes.
Heuristics were tuned on common two-column layouts and are ratio-based, so
most publishers work, but always spot-check a new layout.

**GROBID never becomes available.** Two usual causes:

- *Java too new.* The build fails with
  `Unsupported class file major version 65` (or similar) — GROBID's Gradle
  needs **JDK ≤ 17**. Use the bundled-JDK recipe in
  [Installation](installation.md#grobid-primary-extractor); Rhapsode
  automatically uses a `jdk/` subdir inside the GROBID home.
- *Wrong `home`.* `[grobid] home` must point at the built source tree
  (the directory containing `grobid-home/` and `grobid-service/`).

**GROBID is eating RAM.** Its JVM holds 2–4 GB while running. Rhapsode
stops a service it started after `[grobid] idle_stop_s` (default 10 min)
and restarts it on demand — lower that if memory is tight.

## Audio & playback

**Why is the read-along audio `.m4a` but the CLI output `.mp3`?** MP3 is
universal for podcast players, but VBR MP3 seeking drifts by seconds in
browsers — fatal for word-level sync. The viewer therefore uses AAC/m4a,
which seeks sample-accurately.

**I pressed Ctrl+C mid-generation.** Safe: the encoder is detached from
terminal signals, the partial file is discarded, and the paper resumes
cleanly on the next server start.

## Library & server

**Where is my data?** Everything lives under `[library] path`: imported
PDFs, generated `.readalong` bundles, and `library.json` (positions,
playlists, order). Back up that folder and you have everything.

**Adding the same PDF twice?** Detected by content hash — no duplicate.
**Regenerate** (hover/right-click a card) re-runs extraction + synthesis
in place, e.g. after changing voice or fixing GROBID.

**The server exited by itself.** That's `[gui] idle_exit_min` doing its
job. Set it to `0` to disable, or pair it with
`packaging/rhapsode.service` so the next request starts the server again.

**What's using my GPU/RAM right now?** `GET /api/status` reports model and
GROBID residency; both idle away automatically
([lifecycle keys](configuration.md#tts)).

## Zotero plugin

**Menu items missing / nothing happens.** Enable **Help → Debug Output
Logging → View Output** and look for `[rhapsode]` lines — they trace server
discovery, uploads, and tab handling. After upgrading Rhapsode, restart the
server so the plugin and server match. The old *paper2audio* plugin is
removed automatically when the Rhapsode plugin first runs.

**A collection times out.** Metadata comes straight from Zotero and papers
are queued in seconds; generation itself then runs one paper at a time in
the background — watch progress in the library tab.

## Still stuck?

[Open an issue](https://github.com/saimlau/rhapsode/issues) with the paper
(or its layout description), your platform, and the relevant terminal or
`[rhapsode]` debug output.
