"""OCR for scanned papers: page images in, blocks out.

A scanned PDF has no text layer, so `_gather_blocks` finds nothing and the
paper cannot be narrated. This module renders the pages, sends them to a GPU
OCR endpoint (modal_ocr_app.py), and rebuilds the SAME block structure the
normal path produces — so everything downstream (reading order, labelling,
sentence units, read-along rectangles) works unchanged.

Word boxes are kept as the source of truth rather than writing a synthetic
text layer into the PDF: the viewer highlights each word as it is spoken, and
re-deriving boxes from inserted text drifts against the page image.

The blocks carry no font metadata, so reflow's font-size/typeface split rules
cannot apply. Line HEIGHT stands in for font size, which keeps the same
"a layout change starts a new block" behaviour on scans.
"""

import base64
import json
import urllib.error
import urllib.request

DPI = 200                # enough for 9-10pt body text without huge payloads
PAGES_PER_REQUEST = 4    # matches modal_ocr_app.MAX_PAGES
LINE_OVERLAP = 0.5       # words share a line when their boxes overlap this much
BLOCK_GAP_RATIO = 1.6    # a gap this many times the line pitch starts a block
SIZE_DELTA_RATIO = 0.25  # ...as does a line-height change this large


class OCRError(RuntimeError):
    pass


def enabled(cfg):
    return bool((cfg or {}).get("enabled") and (cfg or {}).get("modal_endpoint"))


def needs_ocr(doc, min_chars=200):
    """True for a PDF with no usable text layer — the scanned-page case."""
    try:
        return len("".join(p.get_text() for p in doc).split()) < min_chars // 5
    except Exception:
        return False


def _post(endpoint, payload, cfg, timeout=300):
    headers = {"Content-Type": "application/json"}
    tok_id = (cfg.get("modal_token_id") or "").strip()
    tok_secret = (cfg.get("modal_token_secret") or "").strip()
    if tok_id or tok_secret:
        # Modal proxy auth needs BOTH halves; half a pair yields an opaque 401
        if not (tok_id and tok_secret):
            raise OCRError("[ocr] modal_token_id and modal_token_secret must "
                           "both be set (Modal proxy auth needs the pair)")
        headers["Modal-Key"], headers["Modal-Secret"] = tok_id, tok_secret
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise OCRError(f"OCR endpoint HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise OCRError(f"OCR endpoint unreachable: {e}") from None


def words_for(doc, cfg):
    """[(page, x0, y0, x1, y1, text)] for every page, in PDF points."""
    endpoint = (cfg.get("modal_endpoint") or "").strip()
    if not endpoint:
        raise OCRError("[ocr] modal_endpoint is not set (deploy "
                       "modal_ocr_app.py first)")
    import fitz
    zoom = DPI / 72.0
    out = []
    for start in range(0, doc.page_count, PAGES_PER_REQUEST):
        batch = list(range(start, min(start + PAGES_PER_REQUEST, doc.page_count)))
        images = [base64.b64encode(
            doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
        ).decode() for i in batch]
        data = _post(endpoint, {"pages": images}, cfg)
        if data.get("error"):
            raise OCRError(f"OCR endpoint error: {data['error']}")
        for pno, page in zip(batch, data.get("pages", [])):
            rect = doc[pno].rect
            for w in page.get("words", []):
                x0, y0, x1, y1 = w["bbox"]      # relative to the page image
                out.append((pno, x0 * rect.width, y0 * rect.height,
                            x1 * rect.width, y1 * rect.height, w["text"]))
    return out


def _lines(words):
    """Group a page's words into lines by vertical overlap, then read each
    line left to right. OCR returns words in detection order, which is not
    reading order."""
    lines = []
    for w in sorted(words, key=lambda w: (w[2], w[1])):
        _, x0, y0, x1, y1, _t = w
        placed = False
        for ln in lines:
            ly0, ly1 = ln["y0"], ln["y1"]
            overlap = min(y1, ly1) - max(y0, ly0)
            if overlap > LINE_OVERLAP * min(y1 - y0, ly1 - ly0):
                ln["words"].append(w)
                ln["y0"], ln["y1"] = min(ly0, y0), max(ly1, y1)
                placed = True
                break
        if not placed:
            lines.append({"y0": y0, "y1": y1, "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w[1])
    return sorted(lines, key=lambda l: l["y0"])


def blocks_from_words(words, start_id=0):
    """Rebuild reflow-shaped blocks from OCR words.

    Same rule as the text path: a line that breaks the run's rhythm starts a
    new block. Font metadata does not exist here, so line height stands in for
    font size — a heading set larger, or a paragraph after a gap, still splits.
    """
    blocks = []
    by_page = {}
    for w in words:
        by_page.setdefault(w[0], []).append(w)
    for pno in sorted(by_page):
        run, pitch, prev = [], None, None
        for ln in _lines(by_page[pno]):
            h = ln["y1"] - ln["y0"]
            if prev is not None:
                gap = ln["y0"] - prev["y0"]
                ph = prev["y1"] - prev["y0"]
                cut = (ph > 0 and abs(h - ph) / ph > SIZE_DELTA_RATIO)
                if not cut and pitch and gap > BLOCK_GAP_RATIO * pitch:
                    cut = True
                if cut:
                    blocks.append(_block(run, pno, start_id + len(blocks)))
                    run, pitch = [], None
                elif gap > 0.5 and pitch is None:
                    pitch = gap
            run.append(ln)
            prev = ln
        if run:
            blocks.append(_block(run, pno, start_id + len(blocks)))
    return [b for b in blocks if b and b["text"].strip()]


def _block(lines, pno, bid):
    words = [w for ln in lines for w in ln["words"]]
    if not words:
        return None
    return {"id": bid, "page": pno,
            "x0": min(w[1] for w in words), "y0": min(w[2] for w in words),
            "x1": max(w[3] for w in words), "y1": max(w[4] for w in words),
            "text": "\n".join(" ".join(w[5] for w in ln["words"])
                              for ln in lines),
            "words": [(w[0], round(w[1], 2), round(w[2], 2), round(w[3], 2),
                       round(w[4], 2), w[5]) for w in words]}


def blocks_for(doc, cfg):
    """The whole path: render -> OCR -> blocks ready for reflow."""
    return blocks_from_words(words_for(doc, cfg))
