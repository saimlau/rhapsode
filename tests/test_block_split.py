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
