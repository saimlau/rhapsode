"""Rhapsode TTS on Modal — run Kokoro synthesis on YOUR OWN Modal account.

For machines without a usable GPU: deploy this app once, then point
Rhapsode at it. Costs land on your Modal account; Modal's free monthly
credits cover hundreds of hours of generated audio, and the container
scales to zero when idle.

Setup (one time):
    pip install modal
    modal setup                      # authenticate your Modal account
    modal deploy modal_app.py        # prints the endpoint URL
    modal token new ...              # or create a proxy-auth token in the
                                     # dashboard if you enable proxy auth

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
REQUIRES_PROXY_AUTH = False

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
)


@app.cls(image=image, gpu="T4", scaledown_window=120, timeout=600)
class KokoroTTS:
    @modal.enter()
    def load(self):
        from kokoro import KPipeline
        self.pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M",
                                  device="cuda")

    @modal.fastapi_endpoint(method="POST",
                            requires_proxy_auth=REQUIRES_PROXY_AUTH)
    def tts(self, req: dict) -> dict:
        import numpy as np
        texts = req.get("texts") or []
        voice = req.get("voice", "af_heart")
        speed = float(req.get("speed", 1.0))
        results = []
        too_long = [i for i, t in enumerate(texts) if len(str(t)) > 5000]
        if too_long:
            return {"error": f"texts {too_long} exceed 5000 chars — split "
                             "them client-side", "results": []}
        for text in texts[:64]:
            waves, words = [], []
            offset = 0.0
            for item in self.pipeline(str(text), voice=voice,
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
        return {"sample_rate": 24000, "results": results}
