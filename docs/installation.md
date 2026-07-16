# Installation

## Prerequisites

- **Python 3.11+** (the config loader uses the stdlib `tomllib`)
- **ffmpeg** — audio encoding
- **espeak-ng** — Kokoro's grapheme-to-phoneme fallback
- A GPU is strongly recommended for synthesis speed (NVIDIA CUDA or
  Apple Silicon); CPU works but is several times slower than realtime on
  long papers.

```bash
# Debian/Ubuntu
sudo apt install ffmpeg espeak-ng
# macOS
brew install ffmpeg espeak-ng
```

## Install

```bash
git clone https://github.com/saimlau/rhapsode && cd rhapsode
python3 -m venv .venv
.venv/bin/pip install pymupdf kokoro soundfile requests fastapi uvicorn python-multipart
```

The `./rhapsode` launcher (or `rhapsode.bat` on Windows) runs the pipeline
with the venv's Python — no activation needed.

The first synthesis downloads the ~330 MB Kokoro-82M model from Hugging Face
into its cache; every run after that is fully offline.

Then copy `config.example.toml` to `config.toml` and set at least the
library path — see the [configuration reference](configuration.md).

## GROBID (primary extractor)

Rhapsode uses [GROBID](https://github.com/kermitt2/grobid) — the standard ML
pipeline for scholarly-PDF structure — when available, and falls back to its
built-in heuristics otherwise. GROBID is optional but recommended: it is what
was trained on thousands of real layouts, and it returns per-sentence
coordinates so read-along highlighting works identically on both extractors.

GROBID needs **Java 17 or older** (its Gradle version cannot parse newer
class files). The clean recipe is a native source install with its own JDK,
which Rhapsode detects automatically:

```bash
cd ~/Documents
wget https://github.com/kermitt2/grobid/archive/refs/tags/0.8.1.tar.gz
tar xf 0.8.1.tar.gz && cd grobid-0.8.1

# bundle a JDK 17 inside the install (no system Java changes)
mkdir jdk && wget -qO- https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse \
  | tar xz -C jdk --strip-components=1

JAVA_HOME=$PWD/jdk ./gradlew clean install -x test
```

Point `config.toml` at it:

```toml
[grobid]
home = "~/Documents/grobid-0.8.1"
```

That's all — Rhapsode starts the service on demand, stops it after
`idle_stop_s` of inactivity (the JVM holds 2–4 GB RAM), and restarts it for
the next paper. If you prefer to manage GROBID yourself (or run it on another
machine), set `autostart = false` and point `url` at your instance.

## Platform notes

### Linux

First-class: everything above plus the optional
[Speech Dispatcher voice](system-voice.md) and the
`packaging/rhapsode.service` systemd user unit (pairs with
`idle_exit_min` for a start-on-demand server).

### macOS

Expected to work (experimental — [testers welcome](https://github.com/saimlau/rhapsode/issues)).
Synthesis uses Apple-Silicon MPS when available, CPU otherwise. GROBID
installs natively the same way (use the macOS Temurin JDK 17 build). The
speechd voice and systemd unit don't apply.

### Windows

Core pipeline expected to work (experimental — testers welcome). Use
`rhapsode.bat` instead of `./rhapsode`:

```bat
rhapsode.bat "some-paper.pdf" --play
```

GROBID has no native Windows build, so extraction automatically falls back
to the built-in heuristics — or run GROBID in WSL/Docker and set
`[grobid] url` (autostart is skipped on Windows either way). The speechd
voice and systemd unit don't apply.
