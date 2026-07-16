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
