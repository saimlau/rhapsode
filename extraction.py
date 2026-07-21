"""Coordinate-preserving text extraction for Rhapsode.

MappedText pairs a string with per-character (page, bbox) metadata so the
cleaning regexes keep text and PDF coordinates aligned. After cleaning,
each sentence can report exactly which rectangles it occupies on which
pages — the basis of the read-along sync manifest.
"""

import unicodedata
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


# Greek blocks: basic (α-ω, Α-Ω) plus the extended range used in maths, and
# U+00B5 MICRO SIGN — visually identical to μ, and PDFs use the two
# interchangeably, so the same-looking text must narrate the same way
GREEK_LETTERS = "\u0370-\u03ff\u1f00-\u1fff\u00b5"

# Mathematical Alphanumeric Symbols: the italic/bold/script letters a Word or
# LaTeX equation leaves in the text layer. 𝑘 is a k and 𝛼 is an alpha, but a
# speech engine has no name for either and falls back to spelling the code
# point — "letter one D four five eight". NFKC maps each to the plain letter
# it is a variant of.
# Built as a char->char table rather than a regex callback: MappedText.sub
# takes a template, and every fold here is exactly one char to one char, so a
# translation preserves the char/bbox mapping for free.
MATH_ALPHANUMERIC = {
    chr(cp): unicodedata.normalize("NFKC", chr(cp))
    for cp in range(0x1D400, 0x1D800)
    if unicodedata.normalize("NFKC", chr(cp)) != chr(cp)
    and len(unicodedata.normalize("NFKC", chr(cp))) == 1
}

# Scripts that cannot appear in a paper this narrator can read, and in
# practice never do: they are mojibake. Word writes equations in Cambria Math
# with a broken ToUnicode map, so "x[k+1]" arrives as "𝑥ሾ𝑘൅1ሿ" — the brackets
# decoded as Ethiopic, "=" as Malayalam. Left in, espeak spells each one out
# ("ethiopic letter...") and even switches voice mid-sentence. Dropping them
# cannot lose anything a English-language narrator could have said.
UNREADABLE_SCRIPTS = re.compile(
    "[\u0900-\u0dff"      # Devanagari … Sinhala (incl. Malayalam, Tamil, Oriya)
    "\u1200-\u139f"       # Ethiopic
    "\u1100-\u11ff"       # Hangul Jamo
    "\uac00-\ud7af]")     # Hangul syllables


def clean_mapped(mt):
    mt = mt.translate_chars(CHAR_FIXES)
    mt = mt.sub(r"(\w)-\n(\w)", r"\1\2")      # de-hyphenate line breaks
    mt = mt.sub(r"\s*\n\s*", " ")             # join wrapped lines
    mt = mt.sub(CITATION_RE, "")              # [12], [8, 13-15]
    mt = mt.sub(r"(\d)\s*–\s*(\d)", r"\1 to \2")
    # Fold maths letters to plain ones BEFORE the Greek rules below, so a
    # folded 𝛼 is treated as the α it is
    mt = mt.translate_chars(MATH_ALPHANUMERIC)
    mt = mt.sub(UNREADABLE_SCRIPTS, " ")
    # A Greek letter glued to its subscript is one token to the phonemizer,
    # which then pronounces the pair as a word: "εx" -> "epsilonks", "σy" ->
    # "sigma-ee". espeak reads the letters themselves correctly, so only the
    # boundary needs marking. mt.sub keeps the char/bbox mapping in step, so
    # the read-along still highlights the original glyphs.
    mt = mt.sub(f"([{GREEK_LETTERS}])([A-Za-z0-9])", r"\1 \2")
    mt = mt.sub(f"([A-Za-z0-9])([{GREEK_LETTERS}])", r"\1 \2")
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

def _body_font_size(doc, max_pages=None):
    weights = {}
    for page in list(doc)[:max_pages]:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    key = round(span["size"], 1)
                    weights[key] = weights.get(key, 0) + len(span["text"])
    return max(weights, key=weights.get) if weights else 0.0


def _block_mapped(block, page_no):
    """(MappedText, dominant size, bold fraction, line count, bold prefix).

    bold prefix = char count of the leading run of bold spans on the first
    line — run-in headings ('1. INTRODUCTION Craniofacial...') live there."""
    size_weight = {}
    chars, metas = [], []
    bold_chars = total_chars = 0
    bold_prefix, prefix_open = 0, True
    for line_no, line in enumerate(block["lines"]):
        if chars:
            chars.append("\n")
            metas.append(None)
        for span in line["spans"]:
            key = round(span["size"], 1)
            size_weight[key] = size_weight.get(key, 0) + len(span["chars"])
            total_chars += len(span["chars"])
            span_bold = bool(span["flags"] & 16)
            if span_bold:
                bold_chars += len(span["chars"])
            if line_no == 0 and prefix_open and span_bold:
                bold_prefix += len(span["chars"])
            else:
                prefix_open = False
            for ch in span["chars"]:
                chars.append(ch["c"])
                metas.append((page_no, tuple(ch["bbox"])))
    size = max(size_weight, key=size_weight.get) if size_weight else 0.0
    bold = bold_chars / total_chars if total_chars else 0.0
    return (MappedText("".join(chars), metas), size, bold,
            len(block["lines"]), bold_prefix)


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


AFFILIATION_RE = re.compile(
    r"\b(Universit|Department|Institute|Laborator|School of|College|"
    r"Center for|Centre|Faculty|Hospital|Academy)", re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.\w")
NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*\.?\s+\S")
SECTION_KEYWORD = re.compile(r"^(abstract|keywords?|index terms)\b", re.I)
PAPER_NUMBER = re.compile(r"^[A-Z]{2,}\d{4}-\d+")


def _strip_superscripts(mt):
    """Text with sub/superscript characters removed — affiliation markers
    ('1,2', 'a,b,c', '∗') are visibly smaller than the name characters.
    Whitespace always survives (space glyphs have degenerate boxes)."""
    heights = sorted(m[1][3] - m[1][1] for c, m in zip(mt.text, mt.meta)
                     if m and not c.isspace())
    if not heights:
        return mt.text
    cutoff = 0.75 * heights[len(heights) // 2]
    return "".join(c for c, m in zip(mt.text, mt.meta)
                   if c.isspace() or m is None
                   or (m[1][3] - m[1][1]) >= cutoff)


def _leading_name_lines(mt):
    """The top lines of a block up to the first affiliation/email line —
    IEEE puts authors and affiliations in one block."""
    end = 0
    for line in mt.text.split("\n"):
        flat = " ".join(line.split())
        if AFFILIATION_RE.search(flat) and not _looks_like_names(flat):
            break
        if EMAIL_RE.search(flat):
            break
        end += len(line) + 1
    return mt.slice(0, min(end, len(mt))).strip()


def _is_author_list(text):
    return text.count("·") >= 2 and re.search(r"\d(?:,\d+)*\s*·", text)


def _name_fragment(text):
    """Short wrapped continuation of an author line: 'Glenn Daehn'."""
    words = text.split()
    return (2 <= len(words) <= 6 and len(text) < 40
            and all(w[0].isalpha() and w[0].isupper() for w in words))


def _name_words(part):
    """Tokens that plausibly belong to a name: drops detached affiliation
    markers ('2', 'a', '∗') that survive superscript stripping."""
    return [w for w in part.split()
            if not w.isdigit() and not (len(w) == 1 and not w.isupper())]


def _looks_like_names(text):
    """Author line: several 'First Last'-shaped parts joined by commas/and."""
    if len(text) > 250:
        return False
    text = re.sub(r"\([^)]*\)", " ", text)  # honorifics: "(Member, IEEE)"
    parts = [p.strip(" .∗*†‡") for p in re.split(r",|\band\b", text, flags=re.I)]
    parts = [p for p in parts if len(p) > 2]
    def namish(p):
        words = _name_words(p)
        return (2 <= len(words) <= 4
                and all(w[0].isalpha() and w[0].isupper() for w in words))
    n = sum(map(namish, parts))
    return n >= 2 and n >= 0.6 * len(parts)


def _is_affiliation(text):
    """Affiliation/correspondence block on the first page."""
    hits = len(AFFILIATION_RE.findall(text))
    return (EMAIL_RE.search(text) is not None or hits >= 2
            or (hits >= 1 and bool(re.match(r"^[0-9a-f]\b", text))))


def _caps_heading(flat):
    """ALL-CAPS section headings ('REFERENCES', '1. INTRODUCTION')."""
    letters = [c for c in flat if c.isalpha()]
    return (len(flat) >= 6 and len(letters) >= 5
            and sum(c.isupper() for c in letters) / len(letters) >= 0.85)


def _collapse_spaced(flat):
    """Elsevier letter-spaced headings: 'a b s t r a c t' -> 'abstract'."""
    if re.fullmatch(r"(?:\w )+\w", flat):
        return flat.replace(" ", "")
    return None


def _strip_tex(s):
    """PDF metadata titles sometimes carry LaTeX macro residue."""
    return re.sub(r"\\[A-Za-z@]+\s*", "", s or "").strip()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _parse_authors(mt):
    """Author names from a MappedText author line (superscripts stripped)."""
    text = re.sub(r"[‐‑‒­]", "-", " ".join(_strip_superscripts(mt).split()))
    text = re.sub(r"\([^)]*\)", " ", text)
    parts = (text.split("·") if "·" in text
             else re.split(r",|\band\b", text, flags=re.I))
    names = []
    for part in parts:
        words = [re.sub(r"\d+$", "", w) for w in _name_words(part)]
        name = " ".join(w for w in words if w).strip(" .∗*†‡")
        if len(name) > 1:
            names.append(name)
    return ", ".join(names)


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


def extract_segments(pdf_path, max_pages=None):
    """Return (segments, found_references, meta).

    segments: list of (kind, MappedText) in reading order, kind in
    {'heading', 'body'}; meta: {'title', 'authors', 'year'}.
    """
    doc = fitz.open(pdf_path)
    body_size = _body_font_size(doc, max_pages)
    segments = []
    meta = {"title": None, "authors": None,
            "year": _page_year(doc[0]) if len(doc) else None}
    title_size = 0.0
    title_idx = -1        # segment index of the title (for multi-block titles)
    title_locked = False  # metadata-confirmed title beats size-based choice
    saw_content = False   # page-0 front matter (authors/affiliations) ends
                          # at the first section heading or long prose block
    meta_title = _norm(_strip_tex(doc.metadata.get("title")))[:60]
    if len(meta_title) < 15:
        meta_title = ""

    # journal names and banners repeat in later pages' running headers —
    # collect those texts to disqualify look-alike page-0 headings as titles
    header_texts = []
    for page in list(doc)[1:5]:
        height = page.rect.height
        for block in page.get_text("dict")["blocks"]:
            if block["type"] == 0 and (block["bbox"][3] < 0.07 * height
                                       or block["bbox"][1] > 0.92 * height):
                text = " ".join("".join(s["text"] for l in block["lines"]
                                        for s in l["spans"]).split()).lower()
                if text:
                    header_texts.append(text)

    def is_banner(flat):
        if BANNERS.match(flat) or PAPER_NUMBER.match(flat):
            return True
        # short heading repeated in running headers = journal name; long ones
        # are excluded because IEEE-style headers repeat the paper title
        fl = flat.lower()
        return 6 <= len(fl) <= 45 and any(fl in h for h in header_texts)

    for page_no, page in enumerate(doc):
        if max_pages is not None and page_no >= max_pages:
            break  # metadata-only fast pass (ingest): front matter suffices
        height = page.rect.height
        blocks = [b for b in page.get_text("rawdict")["blocks"] if b["type"] == 0]
        for block in _order_blocks(blocks, page.rect.width):
            _, y0, _, y1 = block["bbox"]
            if y1 < 0.07 * height or y0 > 0.92 * height:
                continue  # running header / footer zone
            mt, size, bold, n_lines, bold_prefix = _block_mapped(block, page_no)
            flat = " ".join(mt.text.split())
            if not flat:
                continue
            small = size < body_size - 1.1
            front = page_no == 0 and not saw_content
            if small and not front:
                continue  # captions, footnotes, received/copyright lines
            if flat.startswith("©"):
                continue  # copyright lines

            # references heading glued mid-block to the preceding section
            stop = re.search(r"(?m)^\s*(references|bibliography|literature"
                             r"\s+cited)\s*$", mt.text, re.I)
            if stop:
                head = mt.slice(0, stop.start()).strip()
                if head.text:
                    segments.append(("body", head))
                return segments, True, meta

            # run-in heading: bold lead on the first line, prose follows
            # ("1. INTRODUCTION Craniomaxillofacial...", "ABSTRACT Orthog...")
            if 3 <= bold_prefix <= 80 and len(mt) - bold_prefix > 30:
                prefix = " ".join(mt.text[:bold_prefix].split())
                if (NUMBERED_HEADING.match(prefix) or _caps_heading(prefix)
                        or SECTION_KEYWORD.match(prefix)):
                    if STOP_HEADINGS.match(prefix):
                        return segments, True, meta
                    rest = mt.slice(bold_prefix, len(mt)).strip()
                    rest = rest.sub(r"^[:.\s—–-]+", "")
                    if re.match(r"^(keywords?|index terms)", prefix, re.I):
                        rest = rest.sub(r"\s*[·;]\s*", ", ")
                    saw_content = True
                    segments.append(("heading", mt.slice(0, bold_prefix).strip()))
                    segments.append(("body", rest))
                    continue

            stripped = " ".join(_strip_superscripts(mt).split())
            if front and _is_author_list(flat):
                meta["authors"] = _parse_authors(mt)
                continue
            if front and meta["title"] is not None:
                names = _leading_name_lines(mt)
                nstripped = " ".join(_strip_superscripts(names).split())
                if names.text and _looks_like_names(nstripped):
                    parsed = _parse_authors(names)
                    meta["authors"] = (meta["authors"] + ", " + parsed
                                       if meta["authors"] else parsed)
                    continue
                if meta["authors"] and _name_fragment(stripped):
                    meta["authors"] += ", " + _parse_authors(mt)
                    continue
            if front and (AFFILIATION_RE.search(flat)
                          or EMAIL_RE.search(flat)):
                continue  # affiliation / correspondence front matter
            if page_no == 0 and flat.startswith(
                    ("Article history", "ARTICLE INFO", "Available online",
                     "Received ")):
                continue  # Elsevier/IEEE article-info furniture
            if front and small:
                continue  # small front-matter block that isn't an author line
                          # (IEEE small-caps author blocks dip below body size,
                          # so small blocks get an author-capture chance first)

            is_heading = ((size >= body_size + 0.8
                           or (bold >= 0.9 and n_lines <= 2)
                           or (_caps_heading(flat) and n_lines <= 2
                               and len(flat) < 90))
                          and len(flat) < (200 if size >= body_size + 2
                                           else 120))

            if is_heading:
                collapsed = _collapse_spaced(flat)
                if collapsed:  # "a b s t r a c t" -> "abstract"
                    if collapsed.lower() in ("articleinfo", "articlehistory"):
                        continue
                    # Drop ALL whitespace, not just U+0020: `flat` was built
                    # with " ".join(text.split()), so `collapsed` is exactly
                    # the non-whitespace chars. Filtering only spaces left the
                    # "\n" that separates a heading's lines, so a wrapped
                    # spaced heading ("G R A P H I C A L\nA B S T R A C T")
                    # produced one meta entry per line more than characters
                    # and MappedText's 1:1 char/bbox invariant raised.
                    despaced = [(c, m) for c, m in zip(mt.text, mt.meta)
                                if not c.isspace()]
                    mt = MappedText("".join(c for c, _ in despaced),
                                    [m for _, m in despaced])
                    # .capitalize() is not length-preserving for every code
                    # point ("ßx" -> "Ssx"), and the meta list must stay 1:1
                    cap = collapsed.capitalize()
                    if len(cap) == len(collapsed):
                        flat = cap
                        mt = MappedText(flat, mt.meta)
                    else:
                        flat = collapsed

            if is_heading and STOP_HEADINGS.match(flat):
                return segments, True, meta

            if is_heading and re.match(r"(?i)(abstract\b|keywords?\b|"
                                       r"index terms\b|introduction\b|"
                                       r"\d+[.\s]|[ivx]+\.\s)", flat):
                saw_content = True  # front matter ends at the first section

            if is_heading:
                meta_match = (meta_title and _norm(flat)
                              and (meta_title.startswith(_norm(flat)[:40])
                                   or _norm(flat).startswith(meta_title[:40])))
                if page_no == 0 and not title_locked and meta_match:
                    # metadata-confirmed title wins regardless of font size
                    segments = [s for s in segments if s[0] != "heading"]
                    meta["title"], title_size, title_locked = flat, size, True
                    title_idx = len(segments)
                elif (page_no == 0 and not title_locked
                        and size > title_size and not is_banner(flat)):
                    segments = [s for s in segments if s[0] != "heading"]
                    meta["title"], title_size = flat, size
                    title_idx = len(segments)
                elif (page_no == 0 and title_idx == len(segments) - 1
                        and abs(size - title_size) < 0.3):
                    # continuation line of a multi-block title
                    meta["title"] += " " + flat
                    _, tmt = segments[title_idx]
                    segments[title_idx] = ("heading",
                                           tmt + MappedText.plain(" ") + mt)
                    continue
                segments.append(("heading", mt))
            elif page_no == 0 and flat.startswith("Abstract"):
                saw_content = True
                rest = mt.sub(r"^\s*Abstract\s*[:.—–-]*\s*", "").strip()
                if rest.text:  # a bare "Abstract" label announces nothing
                    segments.append(("heading", MappedText.plain("Abstract.")))
                    segments.append(("body", rest))
            elif page_no == 0 and flat.startswith("Keywords"):
                kw = mt.sub(r"^\s*Keywords?\s*[:.—–-]*\s*", "Keywords: ")
                kw = kw.sub(r"\s*[·;]\s*", ", ") + MappedText.plain(".")
                segments.append(("body", kw))
            else:
                if len(flat) > 300:
                    saw_content = True  # long prose = the abstract has begun
                segments.append(("body", mt))

    author = " ".join(_strip_tex(doc.metadata.get("author")).split())
    if len(author) > 3 and (not meta["authors"]
                            or author.count(",") > meta["authors"].count(",")):
        meta["authors"] = author  # PDF metadata is often the full, clean list
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
