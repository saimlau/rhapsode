"""LLM block-classification extractor.

PyMuPDF segments each page into text blocks with bounding boxes. Rather than
ask an LLM to reproduce a paper's text (slow, and a hallucination risk), we
show it only each block's location and first/last sentence and ask which
blocks are readable body content and in what reading order. We then emit the
blocks' *own* original text, so fidelity is exact and read-along rectangles
come straight from the word boxes.

This recovers body text that GROBID's segmentation drops around first-page
footnotes and column/page breaks (each survives as its own block, cleanly
separable), needs no Java service, and runs anywhere PyMuPDF does.
"""

import json
import re

import fitz

import llm

try:
    from extraction import clean_text
except Exception:  # pragma: no cover - extraction always importable in practice
    def clean_text(t):
        return " ".join((t or "").split())

PROMPT = """\
Below are the text blocks of an academic paper, extracted from a PDF. Each
line is one block: its id, page number, top-left (x, y) position, and its
first sentence (head) and last sentence (tail). Left-column x is small,
right-column x is large.

Decide which blocks are READABLE CONTENT to narrate — the title, the abstract,
section/subsection headings, and body paragraphs — and which are FURNITURE to
drop: journal/running headers and footers, author names and affiliations,
the article-info / keywords / history box, corresponding-author footnotes,
emails, DOI and copyright lines, page numbers, figure and table captions,
standalone mathematical equations, table content (cells, rows, numeric data),
and everything from References/Bibliography onward (including acknowledgements,
funding, author contributions, and the reference list).

Return the content blocks in correct reading order (each page: left column
top-to-bottom, then right column; a block whose tail does not end with
sentence punctuation continues into the next content block).

Reply with ONLY a JSON object, no prose:
{"order": [ids of content blocks, in reading order],
 "headings": [subset of order that are the title or a section heading],
 "authors": "comma-separated author names, or empty string",
 "year": publication year as an integer, or null}
"""

_ABBREV = {"fig", "figs", "eq", "eqs", "no", "nos", "vs", "al", "et", "e.g",
           "i.e", "cf", "ref", "refs", "dr", "prof", "mr", "ms", "st", "approx",
           "ca", "sec", "tab", "vol", "pp", "ed", "eds"}
_SENT_END = re.compile(r'[.!?][")\']?$')


def _gather_blocks(doc):
    """[{id,page,x0,y0,x1,y1,text,words}] — words as (page,x0,y0,x1,y1,text),
    assigned to the block whose bbox contains their centre (robust to any
    block-vs-word index mismatch)."""
    blocks = []
    for pi, page in enumerate(doc):
        page_blocks = []
        for b in page.get_text("blocks"):
            x0, y0, x1, y1, txt, _no, typ = b
            if typ != 0 or not txt.strip():
                continue
            page_blocks.append({"id": len(blocks) + len(page_blocks), "page": pi,
                                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                                "text": txt, "words": []})
        for w in page.get_text("words"):
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            for blk in page_blocks:
                if blk["x0"] <= cx <= blk["x1"] and blk["y0"] <= cy <= blk["y1"]:
                    blk["words"].append((pi, round(w[0], 2), round(w[1], 2),
                                         round(w[2], 2), round(w[3], 2), w[4]))
                    break
        blocks.extend(page_blocks)
    return blocks


def _headtail(text, n=150):
    t = " ".join(text.split())
    parts = re.split(r'(?<=[.!?])\s+', t)
    return parts[0][:n], parts[-1][:n]


def _compact(blocks):
    lines = []
    for b in blocks:
        head, tail = _headtail(b["text"])
        lines.append(f'[{b["id"]}] p{b["page"]} x={round(b["x0"])} '
                     f'y={round(b["y0"])} | head: {head!r} | tail: {tail!r}')
    return "\n".join(lines)


def _parse_decision(text):
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise llm.LLMError("LLM did not return JSON")
    return json.loads(m.group(0))


def _tokens(words):
    """(page,x0,y0,x1,y1,text) words -> [(text, [boxes])], de-hyphenating
    line-break splits into one token that keeps both boxes for highlighting."""
    tokens = []
    pending = None
    for pg, x0, y0, x1, y1, wt in words:
        box = (pg, x0, y0, x1, y1)
        if not wt:
            continue
        if pending is not None:
            tokens.append((pending[0] + wt, pending[1] + [box]))
            pending = None
        elif wt.endswith("-") and len(wt) > 1:
            pending = (wt[:-1], [box])
        else:
            tokens.append((wt, [box]))
    if pending is not None:
        tokens.append(pending)
    return tokens


def _is_abbrev(tok):
    core = tok.rstrip('."\')').lower()
    return core in _ABBREV or (len(core) == 1) or core.replace(".", "").isdigit()


def _split_sentences(tokens):
    sents, cur = [], []
    for tok in tokens:
        cur.append(tok)
        if _SENT_END.search(tok[0]) and not _is_abbrev(tok[0]):
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    return sents


def _cluster(boxes):
    """Word boxes -> per-line bands [[page,x0,y0,x1,y1], ...]."""
    bands = []
    for pg, x0, y0, x1, y1 in boxes:
        if bands:
            bp, bx0, by0, bx1, by1 = bands[-1]
            if bp == pg and abs(y0 - by0) <= 0.5 * ((y1 - y0) + (by1 - by0)) / 2:
                bands[-1] = [bp, min(bx0, x0), min(by0, y0),
                             max(bx1, x1), max(by1, y1)]
                continue
        bands.append([pg, x0, y0, x1, y1])
    return bands


HEADING_PAUSE_S = 0.7
PARAGRAPH_PAUSE_S = 0.5
SENTENCE_PAUSE_S = 0.25

# per-window budget (~30k tokens of block summary) and a sanity cap on how
# many windows a document may split into (a ~2000-page book); papers are one
# window, a 400-page thesis is ~12
WINDOW_CHARS = 120_000
MAX_WINDOWS = 80


def _windows(blocks):
    """Split page-ordered blocks into windows each under WINDOW_CHARS of block
    summary. Cutting between blocks is fine — reading order is preserved and
    the global assembly rejoins any paragraph split across a window seam."""
    out, cur, size = [], [], 0
    for b in blocks:
        h, t = _headtail(b["text"])
        est = len(h) + len(t) + 60
        if cur and size + est > WINDOW_CHARS:
            out.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += est
    if cur:
        out.append(cur)
    return out


def _classify(blocks, llm_cfg):
    return _parse_decision(llm.run(PROMPT + _compact(blocks), llm_cfg))


def _classify_windows(windows, llm_cfg):
    """One classification per window. Multiple windows run concurrently (the
    runner calls are I/O-bound); a window that fails contributes nothing rather
    than sinking the whole document."""
    if len(windows) == 1:
        return [_classify(windows[0], llm_cfg)]
    import concurrent.futures as cf

    def one(w):
        try:
            return _classify(w, llm_cfg)
        except Exception:
            return {"order": [], "headings": []}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(one, windows))


_PUA = re.compile("[\ue000-\uf8ff]")  # private-use area: garbled math glyphs


def _narratable(text):
    """False for equation/table debris that would narrate as noise: garbled
    private-use glyphs, or a long span that is mostly non-letters (number
    soup / table rows). Short clean fragments ("See Fig. 3.") still pass."""
    if _PUA.search(text):
        return False
    stripped = text.replace(" ", "")
    if len(stripped) < 3:
        return False
    letters = sum(c.isalpha() for c in stripped)
    if len(stripped) > 15 and letters / len(stripped) < 0.4:
        return False
    return True


def _sentence_units(tokens):
    units = []
    sents = _split_sentences(tokens)
    for j, sent in enumerate(sents):
        text = clean_text(" ".join(t[0] for t in sent))
        if not text or not _narratable(text):
            continue
        boxes = [b for t in sent for b in t[1]]
        last = j == len(sents) - 1
        units.append({"kind": "body", "text": text, "rects": _cluster(boxes),
                      "para_end": last,
                      "pause": PARAGRAPH_PAUSE_S if last else SENTENCE_PAUSE_S})
    if units:
        units[-1]["para_end"] = True
        units[-1]["pause"] = PARAGRAPH_PAUSE_S
    return units


def extract_document(pdf_path, llm_cfg):
    """LLM block-classification extraction. Returns (units, meta).

    Raises llm.LLMError if the model is unavailable or its answer is unusable.
    """
    doc = fitz.open(pdf_path)
    blocks = _gather_blocks(doc)
    if not blocks:
        raise llm.LLMError("no text blocks (scanned/image-only PDF?)")
    byid = {b["id"]: b for b in blocks}

    # A single classification call suits papers (~5-15k tokens). Book-length
    # documents (a 400-page thesis is ~330k tokens of block summary) exceed any
    # context window, so classify page-ordered windows and stitch — a two-level
    # hierarchy (document -> windows -> blocks). Windows classify in parallel;
    # cross-window continuation is handled by the global assembly below.
    windows = _windows(blocks)
    if len(windows) > MAX_WINDOWS:
        raise llm.LLMError(f"document too large ({doc.page_count} pages, "
                           f"{len(windows)} windows > {MAX_WINDOWS})")
    decisions = _classify_windows(windows, llm_cfg)

    order, headings, authors, year = [], set(), "", None
    for d in decisions:
        order += [i for i in d.get("order", []) if i in byid]
        headings |= {i for i in d.get("headings", []) if i in byid}
        authors = authors or (d.get("authors") or "").strip()
        year = year or d.get("year")
    if not order:
        raise llm.LLMError("LLM kept no content blocks")

    units = []
    para = []  # accumulating body tokens across continuation blocks

    def flush():
        if para:
            units.extend(_sentence_units(list(para)))
            para.clear()

    for i in order:
        blk = byid[i]
        if i in headings:
            flush()
            text = clean_text(" ".join(t[0] for t in _tokens(blk["words"])))
            if text:
                units.append({"kind": "heading", "text": text,
                              "rects": _cluster([b for t in _tokens(blk["words"])
                                                 for b in t[1]]),
                              "para_end": False, "pause": HEADING_PAUSE_S})
            continue
        toks = _tokens(blk["words"])
        para.extend(toks)
        # a block that ends a sentence closes the paragraph; one that doesn't
        # (e.g. GROBID's dropped chunk) continues into the next content block
        if toks and _SENT_END.search(toks[-1][0]) and not _is_abbrev(toks[-1][0]):
            flush()
    flush()

    title = ""
    for i in order:  # first heading is the title
        if i in headings:
            title = clean_text(" ".join(t[0] for t in _tokens(byid[i]["words"])))
            break
    meta = {"title": title, "authors": authors, "year": year}
    return units, meta
