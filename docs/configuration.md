# Configuration

Copy `config.example.toml` (in the repo root) to `config.toml` — it is
gitignored, so your paths and tokens never end up in a commit.

Precedence: **CLI flags > `config.toml` > built-in defaults.**

The GUI server reads the file at startup; config changes apply on the next
server start (the CLI reads it fresh on every run).

## `[library]`

| Key | Default | Effect |
|---|---|---|
| `path` | `"~/PaperAudio"` | Where the GUI stores imported PDFs, generated bundles, and `library.json`. |

## `[tts]`

| Key | Default | Effect |
|---|---|---|
| `voice` | `"af_heart"` | Kokoro voice id ([full list](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)). |
| `speed` | `1.0` | Speech speed at synthesis time. (The viewer also has a live playback-speed control that needs no re-synthesis.) |
| `m4a_bitrate` | `"48k"` | Read-along audio bitrate — 48k AAC is transparent for 24 kHz mono speech. |
| `backend` | `"local"` | `"local"` (your GPU/CPU) or `"modal"` (your own Modal account) — see [Compute backends](backends.md). |
| `modal_endpoint` | `""` | The URL printed by `modal deploy modal_app.py`. |
| `modal_token_id` / `modal_token_secret` | `""` | Only if proxy auth is enabled in `modal_app.py`. |
| `park_after_s` | `300` | GUI server: park the model to CPU RAM after this idle time (instant resume, frees ~700 MiB VRAM). |
| `unload_after_s` | `1800` | GUI server: fully unload the model after this idle time (~2 s reload on next use). |

## `[render]`

| Key | Default | Effect |
|---|---|---|
| `dpi` | `150` | Page-image resolution for the read-along view. Higher is crisper and heavier. |

## `[gui]`

| Key | Default | Effect |
|---|---|---|
| `port` | `7717` | Server port; auto-probes upward if busy. |
| `open` | `true` | Open a browser tab when the server starts. |
| `idle_exit_min` | `0` | Exit the server after this many minutes with no real activity (`0` = never). Uploads, playback, and page loads count; idle background tabs don't. Pairs with `packaging/rhapsode.service` for start-on-demand. |

## `[grobid]`

| Key | Default | Effect |
|---|---|---|
| `enabled` | `true` | Use GROBID when reachable; built-in heuristics otherwise. `--no-grobid` forces the heuristics per-run. |
| `url` | `"http://127.0.0.1:8070"` | GROBID service URL (can be remote/WSL/Docker). |
| `autostart` | `true` | Start the service on demand from `home` (Linux/macOS). |
| `home` | *(unset)* | Native GROBID source install; a `jdk/` subdir with JDK 17 is used automatically — see [Installation](installation.md#grobid-primary-extractor). |
| `idle_stop_s` | `600` | Stop a Rhapsode-started GROBID after this idle time (its JVM holds 2–4 GB RAM); it restarts on the next paper. |
