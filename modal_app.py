"""Rhapsode TTS on Modal — run Kokoro synthesis on YOUR OWN Modal account.

For machines without a usable GPU: deploy this app once, then point
Rhapsode at it. Costs land on your Modal account; Modal's free monthly
credits cover hundreds of hours of generated audio, and the container
scales to zero when idle.

Setup (one time):
    pip install modal
    modal setup                      # authenticate your Modal account
    modal deploy modal_app.py        # prints the endpoint URL
    # With REQUIRES_PROXY_AUTH = True (the default below), create a proxy-auth
    # token at https://modal.com/settings/proxy-auth-tokens — NOT
    # `modal token new`, which mints ak-/as- CLI tokens and will 401 here.
    # The pair (wk-.../ws-...) is sent as the Modal-Key / Modal-Secret headers.

config.toml:
    [tts]
    backend = "modal"
    modal_endpoint = "https://<you>--rhapsode-tts-kokorotts-tts.modal.run"
    # only needed if you set REQUIRES_PROXY_AUTH = True below:
    modal_token_id = "wk-..."
    modal_token_secret = "ws-..."

The endpoint receives {"texts": [...], "voice": "af_heart", "speed": 1.0}
and returns per text: 24 kHz mono s16le PCM (base64) plus Kokoro's own
per-word timestamps — the identical timing source the local backend uses,
so read-along sync is unaffected by where synthesis runs.
"""

import base64

import modal

# Set True to require Modal proxy-auth tokens on the endpoint (recommended
# if you mind strangers who guess the URL spending your credits).
REQUIRES_PROXY_AUTH = True
# Concurrent requests per container. Each one needs its OWN Kokoro pipeline:
# a shared pipeline's espeak backend keeps per-call state on the instance and
# races across threads (see tests/test_tts_concurrency.py).
MAX_CONCURRENT = 4

app = modal.App("rhapsode-tts")

def _bake_weights():
    # download Kokoro weights at image-build time so cold starts don't
    # re-fetch ~330 MB on every scale-from-zero
    from kokoro import KPipeline
    KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cpu")


image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("espeak-ng")
    .pip_install("kokoro>=0.9", "soundfile", "numpy")
    .run_function(_bake_weights)
    # @modal.fastapi_endpoint needs FastAPI in the image (Modal stopped adding
    # it implicitly). Kept last on purpose: appending a layer reuses the cached
    # weight-bake above instead of re-downloading ~330 MB of model weights.
    .pip_install("fastapi[standard]")
)


@app.cls(image=image, gpu="T4", scaledown_window=120, timeout=600)
# without this each container takes one request at a time, so every parallel
# request cold-starts a fresh T4 and reloads the model
@modal.concurrent(max_inputs=MAX_CONCURRENT)
class KokoroTTS:
    @modal.enter()
    def load(self):
        import queue

        from kokoro import KPipeline

        # One pipeline per concurrent slot, NOT one shared by all of them.
        # A KPipeline owns a misaki EspeakFallback owning a single phonemizer
        # EspeakBackend, which stores per-call word counts on the instance
        # (_count_txt written before phonemizing, _count_phn after, then
        # compared). Two threads inside that window compare thread A's input
        # count against thread B's output count and the backend raises
        # "number of lines in input and output must be equal" — a 500 that
        # phonemizer itself marks unreachable because it is impossible
        # single-threaded. Checking a pipeline out per request makes the G2P
        # state private again. Kokoro-82M is ~330 MB, so N copies are cheap.
        self.pool = queue.Queue()
        for _ in range(MAX_CONCURRENT):
            self.pool.put(KPipeline(lang_code="a",
                                    repo_id="hexgrad/Kokoro-82M",
                                    device="cuda"))

    @modal.fastapi_endpoint(method="POST",
                            requires_proxy_auth=REQUIRES_PROXY_AUTH)
    def tts(self, req: dict) -> dict:
        import numpy as np
        texts = req.get("texts") or []
        voice = req.get("voice", "af_heart")
        speed = float(req.get("speed", 1.0))
        results = []
        # Modal enforces a hard 150 s HTTP timeout on web endpoints (timeout=
        # above does NOT override it); past that it answers with a 303 that a
        # POST cannot safely replay, so keep one request well inside the window
        too_long = [i for i, t in enumerate(texts) if len(str(t)) > 2000]
        if too_long:
            return {"error": f"texts {too_long} exceed 2000 chars — split "
                             "them client-side", "results": []}
        if len(texts) > 8:
            return {"error": "max 8 texts per request (Modal's 150 s "
                             "web-endpoint timeout); send more batches",
                    "results": []}
        pipeline = self.pool.get()          # private for this request
        try:
            results = self._render(pipeline, texts[:8], voice, speed)
        finally:
            self.pool.put(pipeline)
        return {"sample_rate": 24000, "results": results}

    def _render(self, pipeline, texts, voice, speed):
        import numpy as np
        results = []
        for text in texts:
            waves, words = [], []
            offset = 0.0
            for item in pipeline(str(text), voice=voice,
                                 speed=speed):
                audio = getattr(item, "audio", None)
                if audio is None:
                    _, _, audio = item
                wave = audio.detach().cpu().numpy()
                for tok in getattr(item, "tokens", None) or []:
                    if getattr(tok, "start_ts", None) is not None:
                        words.append({"w": tok.text,
                                      "t0": round(offset + tok.start_ts, 3),
                                      "t1": round(offset + tok.end_ts, 3)})
                offset += len(wave) / 24000
                waves.append(wave)
            pcm = (np.clip(np.concatenate(waves) if waves else
                           np.zeros(1, dtype=np.float32), -1.0, 1.0)
                   * 32767.0).astype("<i2").tobytes()
            results.append({"pcm_b64": base64.b64encode(pcm).decode(),
                            "words": words})
        return results
