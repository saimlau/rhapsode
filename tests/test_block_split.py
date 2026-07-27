"""Blocks are split at layout discontinuities so a table's cells never carry
away the paragraph fused to them. The split must not lose a single word: words
are assigned to blocks by bbox containment, so tighter boxes could strand one."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
import reflow


def _glued_pdf(tmp):
    """A table row (8.5pt) immediately followed by body prose (9.4pt) — the
    Chen et al. 2024 layout that lost a paragraph."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Element type", fontsize=8.5)
    page.insert_text((200, 100), "Tetrahedral", fontsize=8.5)
    page.insert_text((330, 100), "Deformable", fontsize=8.5)
    page.insert_text((72, 110), "Grippers", fontsize=8.5)
    page.insert_text((200, 110), "Hexahedral", fontsize=8.5)
    y = 140                                    # a clear gap, then bigger type
    for line in ["When the gripper for holding engages with and clamps the",
                 "corresponding eyelet, it constrains all active structural",
                 "degrees of freedom within the region of the clamped eyelet."]:
        page.insert_text((72, y), line, fontsize=9.4); y += 11
    p = Path(tmp) / "glued.pdf"; doc.save(str(p))
    return str(p)


def test_table_and_prose_end_up_in_different_blocks():
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open(_glued_pdf(t))
        blocks = reflow._gather_blocks(doc)
        joined = [" ".join(b["text"].split()) for b in blocks]
        table = [x for x in joined if "Element type" in x]
        prose = [x for x in joined if "When the gripper" in x]
        assert table and prose, f"both parts must survive: {joined}"
        assert table[0] != prose[0], \
            f"the table cells must not share a block with the prose: {joined}"


def test_split_loses_no_words():
    """Every word of the page must still land in some block."""
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open(_glued_pdf(t))
        page_words = {w[4] for pg in doc for w in pg.get_text("words")}
        got = set()
        for b in reflow._gather_blocks(doc):
            got |= set(b["text"].split())
        missing = {w for w in page_words if w not in got}
        assert not missing, f"words lost by the split: {missing}"


def test_words_are_still_assigned_to_blocks():
    """Word boxes drive read-along highlighting; a split must keep them."""
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open(_glued_pdf(t))
        blocks = reflow._gather_blocks(doc)
        assert sum(len(b["words"]) for b in blocks) >= 20, \
            "words must still be attached to their block"


def test_a_plain_paragraph_is_not_split():
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open()
        page = doc.new_page()
        y = 100
        for line in ["This is one ordinary paragraph of body text that runs",
                     "across several lines at a single consistent font size",
                     "and an even line spacing throughout the whole block."]:
            page.insert_text((72, y), line, fontsize=10); y += 12
        p = Path(t) / "plain.pdf"; doc.save(str(p))
        blocks = reflow._gather_blocks(fitz.open(str(p)))
        assert len(blocks) == 1, f"a uniform paragraph must stay one block: {blocks}"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)


def _heading_pdf(tmp):
    """A bold section heading at the SAME size as the body text that follows —
    the Chen et al. layout where the heading was swallowed by the paragraph."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "2.2 Nonlinear MPC for Incremental Bending",
                     fontsize=9.4, fontname="helvetica-bold")
    y = 114
    for line in ["With the GP-enhanced predictive model for post-springback",
                 "angle, nonlinear MPC is investigated to generate an optimal",
                 "sequence of bending inputs to achieve the desired geometry."]:
        page.insert_text((72, y), line, fontsize=9.4, fontname="times-roman")
        y += 11
    p = Path(tmp) / "heading.pdf"; doc.save(str(p))
    return str(p)


def test_bold_heading_is_split_from_the_paragraph():
    with tempfile.TemporaryDirectory() as t:
        blocks = reflow._gather_blocks(fitz.open(_heading_pdf(t)))
        joined = [" ".join(b["text"].split()) for b in blocks]
        head = [x for x in joined if x.startswith("2.2 Nonlinear MPC")]
        body = [x for x in joined if "With the GP-enhanced" in x]
        assert head and body, f"both must survive: {joined}"
        assert head[0] != body[0], \
            f"a same-size bold heading must not be glued to the paragraph: {joined}"
        assert "With the GP-enhanced" not in head[0], "heading must stand alone"


def _line(y, spans):
    """A PyMuPDF-shaped line dict: [(text, font, size), ...]."""
    return {"bbox": (72, y, 500, y + 10),
            "spans": [{"text": t, "font": f, "size": sz} for t, f, sz in spans]}


def test_inline_emphasis_does_not_split_a_paragraph():
    """One bold word inside a line is not a typeface change: the split keys on
    each line's DOMINANT face, so a paragraph with emphasis stays whole."""
    lines = [
        _line(100, [("This paragraph runs across several lines of body text",
                     "TimesNewRomanPSMT", 10.0)]),
        _line(112, [("and mentions one ", "TimesNewRomanPSMT", 10.0),
                    ("emphasised", "TimesNewRomanPS-BoldMT", 10.0),
                    (" term but must stay whole here", "TimesNewRomanPSMT", 10.0)]),
        _line(124, [("because the dominant face of every line is unchanged.",
                     "TimesNewRomanPSMT", 10.0)]),
    ]
    assert len(reflow._split_lines(lines)) == 1, \
        "inline emphasis must not shatter a paragraph"


def test_whole_bold_line_does_split():
    """A heading line set entirely in the bold face is a real boundary."""
    lines = [
        _line(100, [("2.2 Nonlinear MPC for Incremental Bending",
                     "Arial-BoldMT", 9.4)]),
        _line(114, [("With the GP-enhanced predictive model for post-springback",
                     "TimesNewRomanPSMT", 9.4)]),
        _line(125, [("angle, nonlinear MPC is investigated to generate inputs.",
                     "TimesNewRomanPSMT", 9.4)]),
    ]
    runs = reflow._split_lines(lines)
    assert len(runs) == 2, f"heading must separate from the body: {len(runs)} runs"
    assert len(runs[0]) == 1 and len(runs[1]) == 2
