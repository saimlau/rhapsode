"""Rhapsode LLM extractor on Modal — run the block-classification model on
YOUR OWN Modal account, for machines with no local GPU and no LLM subscription.

vLLM serves an open model behind an OpenAI-compatible endpoint, so Rhapsode
needs no Modal-specific code: the ordinary `api` runner just points at this
endpoint's URL. (The same config works for any OpenAI-compatible host — a
local vLLM, OpenRouter, together.ai, etc.)

Setup (one time):
    pip install modal
    modal setup
    # the endpoint's bearer key (never stored in this file — it's public):
    modal secret create rhapsode-llm-key VLLM_API_KEY=$(openssl rand -hex 24)
    modal deploy modal_llm_app.py        # prints the endpoint URL

Gemma 4 is ungated, so no Hugging Face token or license acceptance is needed.

config.toml:
    [llm]
    enabled = true
    runner = "api"
    api_base_url = "https://<you>--rhapsode-llm-serve.modal.run/v1"
    api_key = "<the VLLM_API_KEY you put in the rhapsode-llm-key secret>"
    model = "google/gemma-4-12B-it"           # must match MODEL_NAME

This is a TEMPLATE — verify MODEL_NAME (exact Hugging Face id), GPU size, and
the pinned versions for your model before deploying. It follows Modal's
official example: https://modal.com/docs/examples/vllm_inference
"""

import modal

# --- configure these for your model / budget ---------------------------------
# The Hugging Face model id (gated models need the `huggingface` secret above).
# A 12B model fits a single 24-40 GB GPU; the 26B MoE (google/gemma-4-26B-A4B-it)
# needs an H200. Match this to [llm] model in config.toml.
MODEL_NAME = "google/gemma-4-12B-it"   # note the capital B; ungated on HF
GPU = "A100-40GB"
# The endpoint requires an OpenAI Bearer key (matches [llm] api_key) so
# strangers who guess the URL can't spend your credits. The key is NOT stored
# here — this file is tracked in a public repo. Put it in a Modal secret:
#     modal secret create rhapsode-llm-key VLLM_API_KEY=$(openssl rand -hex 24)
# and use that same value for [llm] api_key in config.toml. If the secret is
# missing the server starts unauthenticated, so create it before deploying.
VLLM_PORT = 8000

app = modal.App("rhapsode-llm")

vllm_image = (
    # matches Modal's currently-tested vLLM pairing; an older CUDA/vLLM/torch
    # triple is the wrong thing to gamble a GPU deploy on
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04",
                              add_python="3.12")
    .entrypoint([])
    .uv_pip_install("vllm==0.21.0", "huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1", "HF_XET_HIGH_PERFORMANCE": "1"})
)

# persist weights across cold starts so scale-from-zero doesn't re-download
hf_cache = modal.Volume.from_name("rhapsode-hf-cache", create_if_missing=True)


@app.function(
    image=vllm_image,
    gpu=GPU,
    scaledown_window=5 * 60,   # scale to zero (cost $0) 5 min after last use
    timeout=60 * 60,
    # a bare .modal.run URL is world-reachable: without a ceiling, anyone who
    # finds it can autoscale GPUs against your credits
    max_containers=2,
    volumes={"/root/.cache/huggingface": hf_cache},
    # Gemma 4 is ungated, so no HF token is needed. If you switch to a gated
    # model, create a `huggingface` secret (HF_TOKEN=...) and add it here.
    secrets=[modal.Secret.from_name("rhapsode-llm-key")],
)
# one vLLM server batches many requests; without this each HTTP request would
# cold-start its own GPU container and reload the weights
@modal.concurrent(max_inputs=32)
@modal.web_server(VLLM_PORT, startup_timeout=10 * 60)
def serve():
    import os
    import subprocess
    api_key = os.environ.get("VLLM_API_KEY", "")   # from the Modal secret
    cmd = ["vllm", "serve", MODEL_NAME,
           "--served-model-name", MODEL_NAME,
           "--host", "0.0.0.0", "--port", str(VLLM_PORT),
           # Gemma advertises a 131k context; its KV cache will not fit beside
           # 12B of bf16 weights on a 40 GB card, and vLLM exits before it ever
           # binds the port. Cap it — a block-classification window is ~12k.
           "--max-model-len", "16384",
           "--gpu-memory-utilization", "0.90"]
    if api_key:
        cmd += ["--api-key", api_key]
    else:
        print("WARNING: no VLLM_API_KEY in the rhapsode-llm-key secret — "
              "serving this endpoint UNAUTHENTICATED")
    subprocess.Popen(cmd)
