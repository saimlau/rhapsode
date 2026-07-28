# Compute backends

Synthesis runs locally by default; machines without a usable GPU can run it
serverless on their own [Modal](https://modal.com) account. Extraction,
encoding, the library, and the viewer always stay on your machine — only
text-to-speech moves. Word-level timing comes from the same Kokoro code on
both backends, so read-along sync is identical wherever synthesis runs.

## Local (default)

Device selection is automatic: **CUDA → Apple-Silicon MPS → CPU** (with a
graceful CPU fallback if an op is missing on MPS). CPU works but is several
times slower than realtime; a paper that takes ~1 minute on an RTX-class
GPU can take the better part of an hour on CPU.

In the GUI server the model is lifecycle-managed so an idle library tab
costs nothing: after `park_after_s` idle seconds it parks to CPU RAM
(instant resume, ~700 MiB VRAM freed) and after `unload_after_s` it unloads
entirely (~2 s reload). `GET /api/status` shows the current residency.

## Modal (bring your own account)

`modal_app.py` in the repo root deploys a Kokoro endpoint to **your** Modal
account — costs land on your credits, and Modal's free monthly allowance
covers hundreds of hours of generated audio. The container scales to zero
when idle.

```bash
pip install modal
modal setup                    # authenticate once
modal deploy modal_app.py      # prints the endpoint URL
```

Then in `config.toml`:

```toml
[tts]
backend = "modal"
modal_endpoint = "https://<you>--rhapsode-tts-kokorotts-tts.modal.run"
```

The deployed app runs on a T4 GPU, scales down 120 s after the last
request, and has the model weights baked into the image so cold starts
don't re-download them. Rhapsode batches sentences per request and receives
raw PCM plus Kokoro's per-word timestamps back.

!!! warning "Endpoint privacy"
    By default the endpoint is an unauthenticated URL — obscure, but anyone
    who guesses it can spend your credits. Set
    `REQUIRES_PROXY_AUTH = True` in `modal_app.py`, redeploy, create a
    proxy-auth token in the Modal dashboard, and put its id/secret in
    `modal_token_id` / `modal_token_secret`.

## OCR for scanned papers (Modal, optional)

A scanned PDF has no text layer, so nothing can be extracted and the paper
cannot be narrated at all. With `[ocr]` configured, such a paper is OCR'd on
your own Modal GPU and rebuilt into the same blocks a text PDF produces — so
reading order, labelling and the read-along rectangles all work unchanged.

```bash
modal deploy modal_ocr_app.py     # prints the endpoint URL
```

```toml
[ocr]
enabled = true
modal_endpoint = "https://<you>--rhapsode-ocr-doctrocr-ocr.modal.run"
modal_token_id = "..."
modal_token_secret = "..."
```

It runs **only** when a PDF has essentially no text of its own; a normal paper
never touches it (OCR is strictly worse than a real text layer). If the
endpoint is unreachable the paper simply follows the path it would have taken
without `[ocr]` at all, so a broken OCR never costs you a paper.

It uses docTR (Apache-2.0), which returns words **with boxes**. That matters:
the read-along view highlights each word as it is spoken, so an OCR that
returns only text — a markdown/LaTeX converter, or a vision LLM — would
silently break highlighting. Surya is stronger on academic layout but is
GPL-3.0, which would relicense anything importing it.

Expect OCR'd text to be noisier than a born-digital PDF: hyphenation and
column breaks are recovered less cleanly. It is meant to make an unreadable
paper readable, not to match a real text layer.
