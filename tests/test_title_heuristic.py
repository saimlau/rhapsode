"""Title heuristic: a paragraph's oversized drop-cap initial (a single glyph
larger than the title) must not be mistaken for the paper's title."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
import extraction


def _pdf_with_dropcap():
    doc = fitz.open()
    page = doc.new_page()
    # the real title: multi-word, larger than body but SMALLER than the drop-cap
    page.insert_text((72, 90),
                     "Knowledge Attitudes and Commitment Toward Organ Donation",
                     fontsize=14)
    page.insert_text((72, 118), "W. W. S. Tam, L. K. P. Suen, and H. Y. L. Chan",
                     fontsize=11)
    # an isolated oversized drop-cap initial (bigger than the title)
    page.insert_text((72, 210), "O", fontsize=26)
    # plenty of body at size 10 so the body font size is unambiguous
    body = ("Organ donation is the major component for transplant programs however "
            "the rate of organ donation is relatively low in the region studied. "
            "Understanding the attitude and knowledge of individuals that affect "
            "their willingness to commit as an organ donor is crucial to develop "
            "effective educational programs that raise public awareness. A survey "
            "was distributed to all full time nursing students in the university.")
    y = 250
    for i in range(0, len(body), 68):
        page.insert_text((72, y), body[i:i + 68], fontsize=10)
        y += 14
    return doc


def test_dropcap_initial_is_not_the_title():
    doc = _pdf_with_dropcap()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "dropcap.pdf"
        doc.save(str(p))
        _segs, _stopped, meta = extraction.extract_segments(str(p))
    assert meta["title"] != "O", "a single-glyph drop-cap must not become the title"
    assert (meta["title"] or "").startswith("Knowledge"), \
        f"expected the real title, got {meta['title']!r}"


if __name__ == "__main__":
    test_dropcap_initial_is_not_the_title()
    print("ok")
