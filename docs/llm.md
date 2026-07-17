# LLM extraction repair

GROBID and the built-in heuristics both stumble on body text that wraps
around first-page footnotes, marginalia, and column/page breaks — GROBID can
silently **drop** a chunk and weld the surviving fragments into one sentence,
and the heuristic path can splice a footnote mid-sentence or leave
hyphenation. An optional LLM pass reads the **raw** PDF text (where the
dropped words still are) and reflows it into clean reading order.

Read-along highlighting is preserved: each cleaned sentence's words are
matched back to the PDF's own word boxes to re-derive rectangles. A sentence
whose words don't map back to the PDF is discarded, so the model can't
silently inject text — and if a reflow diverges (a model that summarizes
instead of reflowing), Rhapsode detects the low match rate and keeps the
original extraction.

!!! note "This is for extraction, not voice"
    The LLM only cleans text. Narration is always Kokoro — cloud TTS returns
    no word timestamps and would break the synced highlighting.

## No API key required

The point of this feature is that you don't pay per token. Each vendor's
**agent CLI** authenticates with your existing subscription and runs
headless, and a local model runs free on your GPU:

| Runner | Uses | Notes |
|---|---|---|
| `ollama` | a local Gemma model on your GPU | free, private, fast — the recommended default |
| `claude` | your Claude Pro/Max subscription (Claude Code) | no key; a call runs a full agent turn, so it's slower (minutes) |
| `codex` | your ChatGPT Plus/Pro subscription (Codex CLI) | no key; same agent-turn overhead |
| `api` | a raw API key (Anthropic / OpenAI / Gemini) | pay-per-use / free tier; fastest, needs a key |

`runner = "auto"` picks the first available in that order, so a machine with
Ollama never phones home. (Google's Gemini CLI is EOL and Antigravity is an
IDE with no headless mode, so Gemini is reachable only via the `api` runner
with an AI Studio key.)

## Enable it

```toml
[llm]
enabled = true
runner  = "auto"     # ollama -> claude -> codex -> api
when    = "always"
```

`when` controls how often it runs:

- **`always`** — reflow every paper. This is the only setting that recovers
  text GROBID drops silently: that drop is empirically indistinguishable from
  a correctly dropped figure caption, so it can't be auto-detected. Costs one
  LLM call per paper — seconds on `ollama`/`api`, minutes on the CLIs.
- **`auto`** — reflow only when the extraction left obvious dirt (inline
  emails, DOI lines, hyphenation). Cheap; mainly helps the no-GROBID path;
  **misses** silent GROBID drops.
- **`never`** — off.

Per run, `--llm` forces `always` for one paper and `--no-llm` skips it —
handy for spot-checking with `--text-only`:

```bash
./rhapsode "paper.pdf" --text-only --llm     # see the repaired text
```

## Local Gemma via Ollama (recommended)

```bash
# install ollama (native, no Docker) and pull a Gemma model
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:12b        # fits a 16 GB GPU; 26b MoE is tighter
```

```toml
[llm]
enabled = true
runner  = "ollama"
model   = "gemma4:12b"
```

Gemma 4 (Apr 2026) sizes that suit a 16 GB GPU: **12B** comfortably, the
**26B MoE** (≈4B active/token) more tightly; the 31B dense model is
workstation-class. See [ollama.com/library/gemma4](https://ollama.com/library/gemma4)
for exact tags.

That's it — reflow now runs locally on your GPU, free, for every paper.
