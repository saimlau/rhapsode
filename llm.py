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

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

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
    if cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") \
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


def _run_claude(prompt, cfg, timeout):
    cmd = ["claude", "-p", "-", "--output-format", "text"]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]
    return _run_cli(cmd, prompt, timeout, "claude")


def _run_codex(prompt, cfg, timeout):
    # codex reads the prompt from stdin when given '-'; --json off keeps it text
    cmd = ["codex", "exec", "-"]
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    return _run_cli(cmd, prompt, timeout, "codex")


def _run_ollama(prompt, cfg, timeout):
    model = cfg.get("model") or "gemma3:12b"
    body = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(_ollama_url(cfg) + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read()).get("response", "").strip()
    except urllib.error.HTTPError as e:
        raise LLMError(f"ollama HTTP {e.code}: model '{model}' not pulled?")
    except (urllib.error.URLError, OSError) as e:
        raise LLMError(f"ollama unreachable: {e}")
    if not out:
        raise LLMError("ollama returned no output")
    return out


def _run_api(prompt, cfg, timeout):
    provider = (cfg.get("api_provider") or "").lower()
    key = cfg.get("api_key")
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY"):
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
        key = key or os.environ.get("OPENAI_API_KEY")
        model = cfg.get("model") or "gpt-4o-mini"
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps({"model": model,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
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


def run(prompt, cfg, timeout=None):
    """Run prompt through the resolved runner; raise LLMError if none/failure."""
    runner = resolve(cfg)
    if not runner:
        raise LLMError("no LLM runner available (install ollama/claude/codex "
                       "or set [llm] api_key)")
    timeout = timeout or cfg.get("timeout_s", 120)
    return _RUNNERS[runner](prompt, cfg, timeout)
