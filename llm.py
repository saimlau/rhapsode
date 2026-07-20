"""Pluggable LLM runner for extraction cleanup.

Rhapsode never needs a paid API: the agentic CLIs each vendor ships
authenticate with the user's existing subscription and run headless, and a
local Ollama model runs free on the GPU. A raw API key is the last resort.

Preference order for runner="auto": ollama (local, free) -> claude
(subscription) -> codex (subscription) -> api (key). The first usable one
wins, so a machine with Ollama never phones home.

Every backend takes a prompt string and returns the model's text. Failures
raise LLMError; callers treat that as "skip cleanup", never as fatal.
"""

import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

PREFERENCE = ["ollama", "claude", "codex", "api"]


class LLMError(RuntimeError):
    pass


def _detach():
    # keep a spawned CLI off the terminal's signal group (same reason the
    # ffmpeg encoder is detached): Ctrl+C shouldn't orphan-kill it mid-call
    if os.name == "posix":
        return dict(start_new_session=True)
    return dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


def _ollama_url(cfg):
    return (cfg.get("ollama_url")
            or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")).rstrip("/")


def _ollama_up(cfg):
    try:
        with urllib.request.urlopen(_ollama_url(cfg) + "/api/tags", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def detect(cfg=None):
    """Runners usable right now, in preference order."""
    cfg = cfg or {}
    out = []
    if _ollama_up(cfg):
        out.append("ollama")
    if shutil.which("claude"):
        out.append("claude")
    if shutil.which("codex"):
        out.append("codex")
    # a custom endpoint is itself sufficient — a self-hosted vLLM/Modal server
    # may need no key at all, and without this the runner silently never runs
    if cfg.get("api_key") or cfg.get("api_base_url") \
            or os.environ.get("ANTHROPIC_API_KEY") \
            or os.environ.get("OPENAI_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        out.append("api")
    return out


def resolve(cfg):
    """The runner to use given cfg; '' if none available."""
    want = (cfg.get("runner") or "auto").lower()
    usable = detect(cfg)
    if want == "auto":
        for r in PREFERENCE:
            if r in usable:
                return r
        return ""
    return want if want in usable else ""


def _run_cli(cmd, prompt, timeout, name):
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, **_detach())
    except subprocess.TimeoutExpired:
        raise LLMError(f"{name} timed out after {timeout}s")
    except FileNotFoundError:
        raise LLMError(f"{name} not found on PATH")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip()[-300:]
        raise LLMError(f"{name} exited {p.returncode}: {tail}")
    out = (p.stdout or "").strip()
    if not out:
        raise LLMError(f"{name} returned no output")
    return out


def _run_claude(prompt, cfg, timeout, fmt=None):
    cmd = ["claude", "-p", "-", "--output-format", "text"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    return _run_cli(cmd, prompt, timeout, "claude")


def _run_codex(prompt, cfg, timeout, fmt=None):
    # codex reads the prompt from stdin when given '-'; --json off keeps it text
    cmd = ["codex", "exec", "-"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    return _run_cli(cmd, prompt, timeout, "codex")


def _run_ollama(prompt, cfg, timeout, fmt=None):
    model = cfg.get("model") or "gemma4:12b"
    # /api/chat applies the model's chat template (raw /api/generate does not,
    # so an instruction-tuned model just rambles). think=false is REQUIRED for
    # Gemma 4: its default thinking mode spends the whole token budget on hidden
    # reasoning and returns empty content (ollama#15428, #16583). num_ctx must
    # hold the whole block summary (Ollama defaults to 4096, which silently
    # truncates the instructions). fmt = a JSON schema for constrained decoding.
    payload = {"model": model,
               "messages": [{"role": "user", "content": prompt}],
               "stream": False, "think": cfg.get("ollama_think", False),
               "keep_alive": cfg.get("ollama_keep_alive", "30m"),
               # temperature 0 (greedy): classification must be deterministic —
               # sampling makes the kept-block set vary wildly run to run
               "options": {"num_ctx": cfg.get("ollama_num_ctx", 16384),
                           "temperature": cfg.get("ollama_temperature", 0)}}
    if fmt is not None:
        payload["format"] = fmt
    body = json.dumps(payload).encode()
    req = urllib.request.Request(_ollama_url(cfg) + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = (json.loads(r.read()).get("message") or {}).get("content", "")
    except urllib.error.HTTPError as e:
        raise LLMError(f"ollama HTTP {e.code}: model '{model}' not pulled?")
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(f"ollama unreachable: {e}")
    out = out.strip()
    if not out:
        raise LLMError("ollama returned no output")
    return out


def _run_api(prompt, cfg, timeout, fmt=None):
    provider = (cfg.get("api_provider") or "").lower()
    key = cfg.get("api_key")
    if not provider:
        if cfg.get("api_base_url"):
            provider = "openai"  # a custom endpoint is OpenAI-compatible
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("GEMINI_API_KEY"):
            provider = "gemini"
    if provider == "anthropic":
        key = key or os.environ.get("ANTHROPIC_API_KEY")
        model = cfg.get("model") or "claude-sonnet-5"
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 8192,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"content-type": "application/json", "x-api-key": key or "",
                     "anthropic-version": "2023-06-01"})
        parse = lambda d: "".join(b.get("text", "") for b in d.get("content", []))
    elif provider == "openai":
        # any OpenAI-compatible endpoint: OpenAI itself, or a self-hosted
        # vLLM/Ollama server (e.g. Gemma on your own Modal account) via
        # api_base_url. fmt -> JSON mode so the schema-shaped reply parses.
        key = key or os.environ.get("OPENAI_API_KEY")
        model = cfg.get("model") or "gpt-4o-mini"
        base = (cfg.get("api_base_url") or "https://api.openai.com/v1").rstrip("/")
        if cfg.get("api_base_url"):
            # a self-hosted endpoint can cold-start (loading a 12B model takes
            # minutes) and Modal caps each HTTP hop at 150 s, answering with a
            # 303 the client follows — the 120 s default dies before hop one
            timeout = max(timeout or 0, 900)
        payload = {"model": model, "temperature": 0,
                   "messages": [{"role": "user", "content": prompt}]}
        if fmt is not None:
            # self-hosted vLLM honours a full schema (guided decoding); plain
            # OpenAI's strict json_schema mode would reject this schema shape,
            # so it keeps the laxer JSON mode
            payload["response_format"] = (
                {"type": "json_schema",
                 "json_schema": {"name": "decision", "schema": fmt}}
                if cfg.get("api_base_url") else {"type": "json_object"})
        req = urllib.request.Request(
            base + "/chat/completions", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {key or ''}"})
        parse = lambda d: d["choices"][0]["message"]["content"]
    elif provider == "gemini":
        key = key or os.environ.get("GEMINI_API_KEY")
        model = cfg.get("model") or "gemini-2.5-flash"
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key or ''}",
            data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
            headers={"content-type": "application/json"})
        parse = lambda d: "".join(
            p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
    else:
        raise LLMError("no API provider configured (set [llm] api_provider/api_key)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = parse(json.loads(r.read())).strip()
    except urllib.error.HTTPError as e:
        raise LLMError(f"{provider} API HTTP {e.code}: {e.read()[:200]!r}")
    except (urllib.error.URLError, OSError, KeyError, IndexError) as e:
        raise LLMError(f"{provider} API error: {e}")
    if not out:
        raise LLMError(f"{provider} API returned no output")
    return out


_RUNNERS = {"claude": _run_claude, "codex": _run_codex,
            "ollama": _run_ollama, "api": _run_api}


def _cache_dir(cfg):
    if cfg.get("cache_dir"):
        return Path(cfg["cache_dir"]).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "rhapsode" / "llm"


def _cache_key(runner, prompt, cfg):
    # content-addressed: same paper + runner + model -> same key, no staleness
    h = hashlib.sha256()
    h.update(f"{runner}\0{cfg.get('model', '')}\0".encode())
    h.update(prompt.encode())
    return h.hexdigest()


def release(cfg):
    """Free a locally-hosted model's VRAM. No-op unless the ollama runner is
    in use.

    keep_alive deliberately holds the model resident between papers, but once
    the server stops (or goes idle) nothing should be pinning GPU memory — a
    12B model is ~8 GB, over half a 16 GB laptop GPU, and it keeps the card
    from clocking down. Same reasoning as stopping the GROBID JVM on exit.
    """
    try:
        if not cfg or resolve(cfg) != "ollama":
            return False
        model = cfg.get("model") or "gemma4:12b"
        body = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(_ollama_url(cfg) + "/api/generate",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False  # best-effort: never block shutdown on this


def run(prompt, cfg, timeout=None, fmt=None):
    """Run prompt through the resolved runner; raise LLMError if none/failure.

    fmt, if given, is a JSON schema — the ollama runner uses it for constrained
    decoding. Results are cached on disk keyed by (runner, model, prompt, fmt)
    so re-extracting the same paper is instant. Disable with [llm] cache = false.
    """
    runner = resolve(cfg)
    if not runner:
        raise LLMError("no LLM runner available (install ollama/claude/codex "
                       "or set [llm] api_key)")
    caching = cfg.get("cache", True)
    path = None
    if caching:
        key = _cache_key(runner, prompt + "\0" + json.dumps(fmt, sort_keys=True),
                         cfg)
        path = _cache_dir(cfg) / (key + ".txt")
        try:
            return path.read_text()
        except (OSError, ValueError):
            pass
    out = _RUNNERS[runner](prompt, cfg, timeout or cfg.get("timeout_s", 120), fmt)
    if caching and path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_text(out)
            os.replace(tmp, path)  # atomic
        except OSError:
            pass
    return out
