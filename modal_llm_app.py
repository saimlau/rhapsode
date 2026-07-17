"""Rhapsode LLM extractor on Modal — run the block-classification model on
YOUR OWN Modal account, for machines with no local GPU and no LLM subscription.

vLLM serves an open model behind an OpenAI-compatible endpoint, so Rhapsode
needs no Modal-specific code: the ordinary `api` runner just points at this
endpoint's URL. (The same config works for any OpenAI-compatible host — a
local vLLM, OpenRouter, together.ai, etc.)

Setup (one time):
    pip install modal
    modal setup
    # Gemma is gated on Hugging Face: accept its license, then store a token:
    modal secret create huggingface HF_TOKEN=hf_...
    modal deploy modal_llm_app.py        # prints the endpoint URL

config.toml:
    [llm]
    enabled = true
    runner = "api"
    api_base_url = "https://<you>--rhapsode-llm-serve.modal.run/v1"
    api_key = "<the API_KEY you set below>"   # omit if API_KEY = ""
    model = "google/gemma-3-12b-it"           # must match MODEL_NAME

This is a TEMPLATE — verify MODEL_NAME (exact Hugging Face id, gated), GPU
size, and the pinned versions for your model before deploying. It follows
Modal's official example: https://modal.com/docs/examples/vllm_inference
"""

import modal

# --- configure these for your model / budget ---------------------------------
# The Hugging Face model id (gated models need the `huggingface` secret above).
# A 12B model fits a single 24-40 GB GPU; the 26B MoE (google/gemma-4-26B-A4B-it)
# needs an H200. Match this to [llm] model in config.toml.
MODEL_NAME = "google/gemma-3-12b-it"
GPU = "A100-40GB"
# Set a token to require it as an OpenAI Bearer key (matches [llm] api_key), so
# strangers who guess the URL can't spend your credits. Empty = open endpoint.
API_KEY = ""
VLLM_PORT = 8000

app = modal.App("rhapsode-llm")

vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.4.0-devel-ubuntu22.04",
                              add_python="3.12")
    .entrypoint([])
    .pip_install("vllm==0.9.0", "huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

# persist weights across cold starts so scale-from-zero doesn't re-download
hf_cache = modal.Volume.from_name("rhapsode-hf-cache", create_if_missing=True)


@app.function(
    image=vllm_image,
    gpu=GPU,
    scaledown_window=5 * 60,   # scale to zero (cost $0) 5 min after last use
    timeout=60 * 60,
    volumes={"/root/.cache/huggingface": hf_cache},
    secrets=[modal.Secret.from_name("huggingface")],
)
@modal.web_server(VLLM_PORT, startup_timeout=10 * 60)
def serve():
    import subprocess
    cmd = ["vllm", "serve", MODEL_NAME,
           "--served-model-name", MODEL_NAME,
           "--host", "0.0.0.0", "--port", str(VLLM_PORT)]
    if API_KEY:
        cmd += ["--api-key", API_KEY]
    subprocess.Popen(cmd)
