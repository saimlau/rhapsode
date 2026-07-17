# LLM extractor

GROBID and the built-in heuristics both stumble on body text that wraps around
first-page footnotes and column/page breaks — GROBID can silently **drop** a
chunk and weld the survivors into one sentence. An optional LLM extractor
sidesteps that failure entirely, and needs no Java service.

Instead of asking a model to *reproduce* the paper (slow, and a hallucination
risk), Rhapsode uses PyMuPDF to split each page into text blocks and shows the
model only each block's **location and first/last sentence**. The model
returns which blocks are body content and in what reading order — a few hundred
tokens, not the whole paper. Rhapsode then emits the blocks' **own original
text**, so nothing is invented, and read-along rectangles come straight from
the block word boxes.

On the paper that motivated this, the dropped chunk survives as its own PyMuPDF
block; the model simply keeps it and orders it correctly, recovering text
GROBID discarded — while dropping the footnote, DOI, and running-header blocks
around it. It also extracts the title, authors, and year from the front-matter
blocks, so **no GROBID is needed**.

!!! note "This is for extraction, not voice"
    The LLM only classifies and orders blocks. Narration is always Kokoro —
    cloud TTS returns no word timestamps and would break the synced
    highlighting.

## No API key required

You don't pay per token. Each vendor's **agent CLI** authenticates with your
existing subscription and runs headless, and a local model runs free on your
GPU:

| Runner | Uses | Notes |
|---|---|---|
| `ollama` | a local Gemma model on your GPU | free, private, fast — the recommended default |
| `claude` | your Claude Pro/Max subscription (Claude Code) | no key; a call runs a full agent turn, so it's slower (a couple of minutes) |
| `codex` | your ChatGPT Plus/Pro subscription (Codex CLI) | no key; same agent-turn overhead |
| `api` | a raw API key (Anthropic / OpenAI / Gemini) | pay-per-use / free tier; fastest, needs a key |

`runner = "auto"` picks the first available in that order, so a machine with
Ollama never phones home. (Gemini's CLI is EOL and Antigravity is an IDE with
no headless mode, so Gemini is reachable only via the `api` runner with an AI
Studio key.)

Because the model returns only a short list of block ids, latency is dominated
by the runner, not the paper: seconds on `ollama`/`api`, a couple of minutes on
the CLIs (agent-turn overhead).

## Enable it

```toml
[llm]
enabled = true
runner  = "auto"     # ollama -> claude -> codex -> api
```

When enabled with a runner available, the LLM extractor is **primary**; GROBID
or the heuristics remain the automatic fallback if it's unavailable or returns
too little. Per run, `--llm` forces it on and `--no-llm` skips it — handy for
spot-checking with `--text-only`:

```bash
./rhapsode "paper.pdf" --text-only --llm     # see the LLM-extracted text
```

## Limits

Verified across a spread of papers, block-classification matches or beats
GROBID on body text — clean reading order, no leaked captions, and it recovers
passages GROBID drops. Two things to know:

- **Metadata (title/authors) is best-effort.** The model can truncate a long
  title or miss authors in a long list. In the Zotero flow this doesn't matter
  (Zotero's metadata is authoritative); for direct PDFs, treat the tags as
  approximate.
- **Book-length documents fall back.** A single classification call suits
  papers; a 400-page thesis exceeds any context window, so Rhapsode detects the
  size and falls back to GROBID/heuristics automatically.

Equation and table debris (garbled math glyphs, numeric table rows) is filtered
out of the narration.

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
for exact tags. With Ollama the extractor runs locally, free, in seconds — and
you can turn GROBID off entirely.
