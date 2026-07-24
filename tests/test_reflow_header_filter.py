"""reflow drops running header/footer-zone blocks so the model never keeps a
banner or page footer as content."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
from reflow import _gather_blocks


def test_header_and_footer_blocks_are_dropped():
    doc = fitz.open()
    page = doc.new_page()                       # 612 x 792
    page.insert_text((72, 20), "RUNNING HEADER JOURNAL NAME")     # top margin
    page.insert_text((72, 400), "This is the body text of the paper here.")
    page.insert_text((72, 822), "Author, Journal 8, e123 (2023) 1 of 3")  # bottom
    texts = " ".join(b["text"] for b in _gather_blocks(doc))
    assert "body text" in texts
    assert "RUNNING HEADER" not in texts, "top-margin header must be dropped"
    assert "1 of 3" not in texts, "bottom-margin footer must be dropped"


if __name__ == "__main__":
    test_header_and_footer_blocks_are_dropped(); print("ok")
