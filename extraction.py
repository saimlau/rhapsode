"""Coordinate-preserving text extraction for paper2audio.

MappedText pairs a string with per-character (page, bbox) metadata so the
cleaning regexes keep text and PDF coordinates aligned. After cleaning,
each sentence can report exactly which rectangles it occupies on which
pages — the basis of the read-along sync manifest.
"""

import re

import fitz  # PyMuPDF

STOP_HEADINGS = re.compile(r"^(references|bibliography|literature\s+cited)\b", re.I)
BANNERS = re.compile(r"^(sciencedirect|available online|journal homepage|www\.)", re.I)
CITATION_RE = re.compile(r"\s*\[[0-9,;\s–—-]+\]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(“\"])")
CHAR_FIXES = {"‐": "-", "‑": "-", "‒": "-", "­": "-", "ﬁ": "fi", "ﬂ": "fl",
              " ": " ", " ": " ", " ": " ", " ": " ",
              " ": " "}
GROUP_REF = re.compile(r"\\[1-9]")


class MappedText:
    """A string plus a parallel per-char metadata list ((page, bbox) or None)."""

    __slots__ = ("text", "meta")

    def __init__(self, text="", meta=None):
        self.text = text
        self.meta = list(meta) if meta is not None else [None] * len(text)
        if len(self.text) != len(self.meta):
            raise ValueError("text/meta length mismatch")

    @classmethod
    def plain(cls, text):
        return cls(text, [None] * len(text))

    def __len__(self):
        return len(self.text)

    def __add__(self, other):
        return MappedText(self.text + other.text, self.meta + other.meta)

    def slice(self, start, end):
        return MappedText(self.text[start:end], self.meta[start:end])

    def strip(self):
        start, end = 0, len(self.text)
        while start < end and self.text[start].isspace():
            start += 1
        while end > start and self.text[end - 1].isspace():
            end -= 1
        return self.slice(start, end)

    def translate_chars(self, table):
        """Replace single chars via table; replacement chars inherit the meta."""
        chars, metas = [], []
        for ch, m in zip(self.text, self.meta):
            rep = table.get(ch)
            if rep is None:
                chars.append(ch)
                metas.append(m)
            else:
                for c in rep:
                    chars.append(c)
                    metas.append(m)
        return MappedText("".join(chars), metas)

    def sub(self, pattern, template):
        r"""re.sub with meta tracking. \1..\9 in template copy group chars+meta;
        literal template chars carry no meta."""
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        stream, pos = [], 0
        for m in GROUP_REF.finditer(template):
            if m.start() > pos:
                stream.append(("lit", template[pos:m.start()]))
            stream.append(("ref", int(m.group()[1])))
            pos = m.end()
        if pos < len(template):
            stream.append(("lit", template[pos:]))

        out_text, out_meta, last = [], [], 0
        for m in pattern.finditer(self.text):
            out_text.append(self.text[last:m.start()])
            out_meta.extend(self.meta[last:m.start()])
            for kind, val in stream:
                if kind == "ref":
                    s, e = m.span(val)
                    out_text.append(self.text[s:e])
                    out_meta.extend(self.meta[s:e])
                else:
                    out_text.append(val)
                    out_meta.extend([None] * len(val))
            last = m.end()
        out_text.append(self.text[last:])
        out_meta.extend(self.meta[last:])
        return MappedText("".join(out_text), out_meta)

    def rects(self, line_gap_ratio=0.5, run_gap=10.0):
        """Group char boxes into one rect per visual line: page -> y-overlap
        line clusters -> x-runs split at gaps (column boundaries)."""
        per_page = {}
        for m in self.meta:
            if m:
                per_page.setdefault(m[0], []).append(m[1])
        rects = []
        for page, boxes in sorted(per_page.items()):
            lines = []
            for b in sorted(boxes, key=lambda b: (b[1], b[0])):
                h = max(b[3] - b[1], 0.1)
                for ln in lines:
                    overlap = min(ln["y1"], b[3]) - max(ln["y0"], b[1])
                    if overlap > line_gap_ratio * min(h, ln["y1"] - ln["y0"]):
                        ln["boxes"].append(b)
                        ln["y0"], ln["y1"] = min(ln["y0"], b[1]), max(ln["y1"], b[3])
                        break
                else:
                    lines.append({"y0": b[1], "y1": b[3], "boxes": [b]})
            for ln in lines:
                run = []
                for b in sorted(ln["boxes"], key=lambda b: b[0]):
                    if run and b[0] - run[-1][2] > run_gap:
                        rects.append(_union(page, run))
                        run = []
                    run.append(b)
                if run:
                    rects.append(_union(page, run))
        return rects


def _union(page, boxes):
    return [page,
            round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2),
            round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)]


def clean_mapped(mt):
    mt = mt.translate_chars(CHAR_FIXES)
    mt = mt.sub(r"(\w)-\n(\w)", r"\1\2")      # de-hyphenate line breaks
    mt = mt.sub(r"\s*\n\s*", " ")             # join wrapped lines
    mt = mt.sub(CITATION_RE, "")              # [12], [8, 13-15]
    mt = mt.sub(r"(\d)\s*–\s*(\d)", r"\1 to \2")
    mt = mt.sub(r"\s{2,}", " ")
    return mt.strip()


def clean_text(text):
    return clean_mapped(MappedText.plain(text)).text


def split_sentences(mt, limit=450):
    """Sentence-aligned MappedText pieces; over-long sentences split at commas."""
    spans, start = [], 0
    for m in SENTENCE_SPLIT.finditer(mt.text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(mt)))
    out = []
    for a, b in spans:
        piece = mt.slice(a, b).strip()
        while len(piece) > limit:
            cut = piece.text.rfind(", ", 0, limit)
            if cut < limit // 3:
                cut = piece.text.rfind(" ", 0, limit)
            if cut <= 0:
                break
            out.append(piece.slice(0, cut + 1).strip())
            piece = piece.slice(cut + 1, len(piece)).strip()
        if piece.text:
            out.append(piece)
    return out


# ---------------------------------------------------------------- extraction

def _body_font_size(doc):
    weights = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    key = round(span["size"], 1)
                    weights[key] = weights.get(key, 0) + len(span["text"])
    return max(weights, key=weights.get) if weights else 0.0


def _block_mapped(block, page_no):
    """(MappedText, dominant font size, bold char fraction, line count)."""
    size_weight = {}
    chars, metas = [], []
    bold_chars = total_chars = 0
    for line in block["lines"]:
        if chars:
            chars.append("\n")
            metas.append(None)
        for span in line["spans"]:
            key = round(span["size"], 1)
            size_weight[key] = size_weight.get(key, 0) + len(span["chars"])
            total_chars += len(span["chars"])
            if span["flags"] & 16:  # bold bit
                bold_chars += len(span["chars"])
            for ch in span["chars"]:
                chars.append(ch["c"])
                metas.append((page_no, tuple(ch["bbox"])))
    size = max(size_weight, key=size_weight.get) if size_weight else 0.0
    bold = bold_chars / total_chars if total_chars else 0.0
    return MappedText("".join(chars), metas), size, bold, len(block["lines"])


def _order_blocks(blocks, page_width):
    """Reading order for a (possibly) two-column page: blocks spanning the
    midline act as flow breaks; between them, left column then right."""
    mid = page_width / 2
    ordered, left, right = [], [], []

    def flush():
        ordered.extend(sorted(left, key=lambda b: b["bbox"][1]))
        ordered.extend(sorted(right, key=lambda b: b["bbox"][1]))
        left.clear()
        right.clear()

    for block in sorted(blocks, key=lambda b: b["bbox"][1]):
        x0, _, x1, _ = block["bbox"]
        if x0 < mid - 20 and x1 > mid + 20:
            flush()
            ordered.append(block)
        elif (x0 + x1) / 2 < mid:
            left.append(block)
        else:
            right.append(block)
    flush()
    return ordered


def _is_author_list(text):
    return text.count("·") >= 2 and re.search(r"\d(?:,\d+)*\s*·", text)


def _name_fragment(text):
    """Short wrapped continuation of an author line: 'Glenn Daehn'."""
    words = text.split()
    return (2 <= len(words) <= 6 and len(text) < 40
            and all(w[0].isalpha() and w[0].isupper() for w in words))


def _looks_like_names(text):
    """Comma-separated author line: several 'First Last'-shaped parts."""
    parts = [p.strip() for p in text.split(",")]
    namish = sum(1 for p in parts
                 if len(p.split()) >= 2
                 and all(w[0].isupper() for w in p.split() if w[0].isalpha()))
    return len(parts) >= 3 and namish >= 2


def _parse_authors(mt):
    """Author names from a MappedText author line. Superscript affiliation
    markers (Springer's '1,2', Elsevier's 'a,b,c') are stripped by glyph
    height — superscripts are visibly smaller than the name characters."""
    heights = sorted(m[1][3] - m[1][1] for m in mt.meta if m)
    if heights:
        cutoff = 0.75 * heights[len(heights) // 2]
        kept = [(c, m) for c, m in zip(mt.text, mt.meta)
                if m is None or (m[1][3] - m[1][1]) >= cutoff]
        text = "".join(c for c, _ in kept)
    else:
        text = mt.text
    text = re.sub(r"[‐‑‒­]", "-", " ".join(text.split()))
    sep = "·" if "·" in text else ","
    names = [re.sub(r"[\d\s*,]+$", "", n).strip(" .") for n in text.split(sep)]
    return ", ".join(n for n in names if len(n) > 1)


def _page_year(page):
    """Publication year: the most frequent plausible year on the page
    (copyright, received/accepted, and issue lines all repeat it)."""
    years = re.findall(r"\b(19[5-9]\d|20[0-3]\d)\b", page.get_text())
    if not years:
        return None
    counts = {}
    for y in years:
        counts[y] = counts.get(y, 0) + 1
    return int(max(counts, key=lambda y: (counts[y], y)))


def extract_segments(pdf_path):
    """Return (segments, found_references, meta).

    segments: list of (kind, MappedText) in reading order, kind in
    {'heading', 'body'}; meta: {'title', 'authors', 'year'}.
    """
    doc = fitz.open(pdf_path)
    body_size = _body_font_size(doc)
    segments = []
    meta = {"title": None, "authors": None,
            "year": _page_year(doc[0]) if len(doc) else None}
    title_size = 0.0

    for page_no, page in enumerate(doc):
        height = page.rect.height
        blocks = [b for b in page.get_text("rawdict")["blocks"] if b["type"] == 0]
        for block in _order_blocks(blocks, page.rect.width):
            _, y0, _, y1 = block["bbox"]
            if y1 < 0.07 * height or y0 > 0.92 * height:
                continue  # running header / footer zone
            mt, size, bold, n_lines = _block_mapped(block, page_no)
            flat = " ".join(mt.text.split())
            if not flat:
                continue
            if size < body_size - 1.1:
                continue  # captions, affiliations, received/copyright lines
            if page_no == 0 and _is_author_list(flat):
                meta["authors"] = _parse_authors(mt)
                continue
            is_heading = ((size >= body_size + 0.8
                           or (bold >= 0.9 and n_lines <= 2))
                          and len(flat) < 120)
            if is_heading and STOP_HEADINGS.match(flat):
                return segments, True, meta
            if is_heading and page_no == 0 and meta["title"] is not None:
                if meta["authors"] is None and _looks_like_names(flat):
                    meta["authors"] = _parse_authors(mt)  # Elsevier author line
                    continue
                if meta["authors"] and _name_fragment(flat):
                    meta["authors"] += ", " + _parse_authors(mt)  # wrapped names
                    continue
            if is_heading:
                if page_no == 0 and size > title_size and not BANNERS.match(flat):
                    meta["title"] = flat
                    title_size = size
                    # publisher banners ("ScienceDirect") sit above the real
                    # title; drop any heading collected before it on page 0
                    segments = [s for s in segments if s[0] != "heading"]
                segments.append(("heading", mt))
            elif page_no == 0 and flat.startswith("Abstract"):
                segments.append(("heading", MappedText.plain("Abstract.")))
                segments.append(("body", mt.sub(r"^\s*Abstract\s*", "")))
            elif page_no == 0 and flat.startswith("Keywords"):
                kw = mt.sub(r"^\s*Keywords\s*", "Keywords: ")
                kw = kw.sub(r"\s*·\s*", ", ") + MappedText.plain(".")
                segments.append(("body", kw))
            else:
                segments.append(("body", mt))
    return segments, False, meta


def merge_continuations(segments):
    """Join body blocks split mid-sentence at column/page breaks. Wrongly
    merging two paragraphs only costs a pause, so bias toward merging."""
    merged = []
    for kind, mt in segments:
        prev_end = merged[-1][1].text.rstrip().rstrip("\"”’)»") if merged else ""
        if (kind == "body" and merged and merged[-1][0] == "body"
                and not prev_end.endswith((".", "!", "?"))):
            merged[-1][1] = merged[-1][1].strip() + MappedText.plain("\n") + mt
        else:
            merged.append([kind, mt])
    return [(kind, mt) for kind, mt in merged]
