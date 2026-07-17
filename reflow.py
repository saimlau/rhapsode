"""LLM reflow of raw PDF text into clean, narratable body units.

GROBID and the heuristic extractor both mishandle body text that wraps
around first-page footnotes, marginalia, and column/page breaks — GROBID
silently drops it. Recovering dropped text requires reading the *raw* PDF
text (the words are there; the structural model lost them), so this module
reflows raw text with an LLM and then re-derives read-along rectangles by
matching each cleaned word back to the PDF's own word boxes.

Everything degrades safely: an LLM failure falls back to the base
extractor, and a sentence whose words can't be located just loses its
highlight (the audio is still correct).
"""

import re
from collections import defaultdict
from difflib import SequenceMatcher

import fitz

import llm

REFLOW_PROMPT = """\
You are extracting the readable body text of an academic paper from raw \
two-column PDF text, to be read aloud by a text-to-speech engine. Return the \
body in correct reading order with these rules:

- Put the paper title on a line starting with "# ".
- Put each section/subsection heading on its own line starting with "## ".
- Put body paragraphs as plain prose, one blank line between paragraphs.
- REMOVE entirely: author names and affiliations, corresponding-author \
footnotes, email addresses, DOI and copyright lines, running headers and \
footers, page numbers, figure and table captions, and in-text citation \
markers such as [12] or (Smith et al., 2020).
- STOP before the References/Bibliography section; do not include it or \
anything after it (acknowledgements, funding, author contributions).
- De-hyphenate words split across line breaks ("re- placement" -> \
"replacement").
- Preserve the exact wording and the reading order of the body. Do NOT \
summarize, paraphrase, add, or reorder sentences. Only join, drop furniture, \
and fix hyphenation.

Output only the cleaned document, nothing else.

RAW PDF TEXT:
"""

_REF_RE = re.compile(r'(?im)^\s*(references|bibliography|references and notes)\s*$')


def _references_cut(raw):
    """Char offset of the References heading, or None."""
    m = None
    for m in _REF_RE.finditer(raw):
        pass  # take the LAST match — in-text "References" mentions come earlier
    return m.start() if m else None


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _pdf_word_stream(doc):
    """[(page, x0, y0, x1, y1, norm), ...] in PyMuPDF reading order.

    Words hyphenated across a line break ("re-" + "placement") are merged so
    they align with the LLM's de-hyphenated output ("replacement"); the merged
    box unions both fragments.
    """
    stream = []
    pending = None  # a word whose raw text ended with '-'
    for pi, page in enumerate(doc):
        for w in page.get_text("words"):
            raw, n = w[4], _norm(w[4])
            if not n:
                continue
            box = [pi, round(w[0], 2), round(w[1], 2),
                   round(w[2], 2), round(w[3], 2), n]
            if pending is not None:
                # merge into the dangling hyphenated fragment
                pending[3] = max(pending[3], box[3])
                pending[4] = max(pending[4], box[4])
                pending[5] += n
                stream.append(pending)
                pending = None
            elif raw.endswith("-") and len(raw) > 1:
                pending = box
            else:
                stream.append(box)
    if pending is not None:
        stream.append(pending)
    return stream


def _cluster(word_rects):
    """Merge matched word boxes into per-line bands: [[page,x0,y0,x1,y1],...]."""
    bands = []
    for pi, x0, y0, x1, y1 in word_rects:
        if bands:
            bp, bx0, by0, bx1, by1 = bands[-1]
            if bp == pi and abs(y0 - by0) <= 0.5 * (y1 - y0 + by1 - by0) / 2:
                bands[-1] = [bp, min(bx0, x0), min(by0, y0),
                             max(bx1, x1), max(by1, y1)]
                continue
        bands.append([pi, x0, y0, x1, y1])
    return bands


def _assign_rects(units, unit_tokens, stream):
    """Globally align the cleaned token stream to the PDF word stream and give
    each unit its clustered rectangles.

    A global alignment (difflib) is robust where a forward cursor is not: the
    cleaned tokens are the PDF tokens minus furniture and minus reordering
    noise, which is exactly a longest-common-subsequence problem. Furniture
    PDF words and unmatched cleaned words (e.g. numbers the LLM normalized)
    simply don't align and are skipped.
    """
    flat = []              # (unit_index, norm_token)
    for ui, toks in enumerate(unit_tokens):
        for t in toks:
            flat.append((ui, t))
    cleaned_norms = [t for _, t in flat]
    pdf_norms = [s[5] for s in stream]

    sm = SequenceMatcher(None, cleaned_norms, pdf_norms, autojunk=False)
    word_rects = defaultdict(list)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            ui = flat[a + k][0]
            pi, x0, y0, x1, y1, _ = stream[b + k]
            word_rects[ui].append((pi, x0, y0, x1, y1))
    for ui, u in enumerate(units):
        u["rects"] = _cluster(word_rects.get(ui, []))


# sentence splitter shared shape with extraction.split_sentences (abbrev-safe)
_SENT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z(])')


def _split_sentences(text):
    text = text.strip()
    if not text:
        return []
    parts = _SENT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _parse_cleaned(cleaned):
    """Cleaned markdown-ish text -> [(kind, text), ...] blocks."""
    blocks = []
    for line in cleaned.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("## "):
            blocks.append(("heading", s[3:].strip()))
        elif s.startswith("# "):
            blocks.append(("heading", s[2:].strip()))
        else:
            blocks.append(("body", s))
    return blocks


# pauses mirror rhapsode.py's constants (kept local to avoid a circular import)
HEADING_PAUSE_S = 0.7
PARAGRAPH_PAUSE_S = 0.5
SENTENCE_PAUSE_S = 0.25


def reflow_document(pdf_path, llm_cfg):
    """Reflow the raw PDF into units [{kind,text,rects,para_end,pause}].

    Raises llm.LLMError if the model is unavailable or fails.
    """
    doc = fitz.open(pdf_path)
    raw = "\n".join(page.get_text() for page in doc)
    cut = _references_cut(raw)
    body_raw = raw[:cut] if cut else raw

    cleaned = llm.run(REFLOW_PROMPT + body_raw, llm_cfg)
    # guard against a truncated/empty generation clobbering a good paper
    if len(_norm(cleaned)) < 0.3 * len(_norm(body_raw)):
        raise llm.LLMError("reflow output implausibly short — likely truncated")

    units = []
    unit_tokens = []

    def _add(unit, text):
        unit_tokens.append([t for t in (_norm(w) for w in text.split()) if t])
        units.append(unit)

    for kind, text in _parse_cleaned(cleaned):
        if kind == "heading":
            _add({"kind": "heading", "text": text, "rects": [],
                  "para_end": False, "pause": HEADING_PAUSE_S}, text)
            continue
        sentences = _split_sentences(text)
        for j, sent in enumerate(sentences):
            last = j == len(sentences) - 1
            _add({"kind": "body", "text": sent, "rects": [], "para_end": last,
                  "pause": PARAGRAPH_PAUSE_S if last else SENTENCE_PAUSE_S}, sent)

    _assign_rects(units, unit_tokens, _pdf_word_stream(doc))
    return units
