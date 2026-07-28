"""OCR path: page images in, reflow-shaped blocks out, so a scanned paper runs
the SAME downstream pipeline as a text PDF. The network call is mocked — what
matters here is that the block synthesis and the fallbacks behave."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
import ocr


def _w(page, x0, y0, x1, y1, text):
    return (page, x0, y0, x1, y1, text)


def test_words_group_into_lines_and_blocks():
    """Two paragraphs separated by a gap become two blocks, each with its
    words in reading order."""
    words = []
    y = 100
    for line in [["The", "first", "paragraph"], ["continues", "here", "plainly"]]:
        x = 50
        for t in line:
            words.append(_w(0, x, y, x + 30, y + 10, t)); x += 35
        y += 12
    y += 40                                   # a clear paragraph gap
    for line in [["A", "second", "paragraph"], ["follows", "the", "gap"]]:
        x = 50
        for t in line:
            words.append(_w(0, x, y, x + 30, y + 10, t)); x += 35
        y += 12
    blocks = ocr.blocks_from_words(words)
    assert len(blocks) == 2, [b["text"] for b in blocks]
    assert blocks[0]["text"].split("\n")[0] == "The first paragraph"
    assert "second paragraph" in blocks[1]["text"]


def test_block_shape_matches_the_text_path():
    words = [_w(0, 50, 100, 80, 110, "Hello"), _w(0, 90, 100, 130, 110, "world")]
    b = ocr.blocks_from_words(words)[0]
    for k in ("id", "page", "x0", "y0", "x1", "y1", "text", "words"):
        assert k in b, f"missing {k}: reflow expects the same block shape"
    assert b["words"] and len(b["words"][0]) == 6, "words are (page,x0,y0,x1,y1,text)"
    assert b["x0"] == 50 and b["x1"] == 130


def test_a_larger_line_starts_its_own_block():
    """No font metadata on a scan, so line height stands in for font size —
    a heading still separates from the paragraph under it."""
    words = [_w(0, 50, 100, 200, 118, "A Big Heading"),          # 18pt tall
             _w(0, 50, 126, 200, 136, "then ordinary body text")] # 10pt tall
    assert len(ocr.blocks_from_words(words)) == 2


def test_words_are_ordered_left_to_right_within_a_line():
    """OCR returns detection order, not reading order."""
    words = [_w(0, 200, 100, 240, 110, "third"),
             _w(0, 50, 100, 90, 110, "first"),
             _w(0, 120, 100, 170, 110, "second")]
    assert ocr.blocks_from_words(words)[0]["text"] == "first second third"


def test_needs_ocr_only_for_a_textless_pdf():
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open(); page = doc.new_page()
        y = 100
        for _ in range(12):          # many short lines, not one that overruns
            page.insert_text((72, y), "real body text on the page here now")
            y += 14
        p = Path(t) / "text.pdf"; doc.save(str(p))
        assert not ocr.needs_ocr(fitz.open(str(p))), "a text PDF must not be OCR'd"
        blank = fitz.open(); blank.new_page()
        assert ocr.needs_ocr(blank), "an empty/scanned page needs OCR"


def test_enabled_requires_both_flag_and_endpoint():
    assert not ocr.enabled(None)
    assert not ocr.enabled({"enabled": True})
    assert not ocr.enabled({"modal_endpoint": "https://x"})
    assert ocr.enabled({"enabled": True, "modal_endpoint": "https://x"})


def test_half_a_token_pair_is_refused():
    """Modal proxy auth needs both halves; one alone yields an opaque 401."""
    try:
        ocr._post("https://x", {}, {"modal_token_id": "only-id"})
        assert False, "must refuse half a token pair"
    except ocr.OCRError as e:
        assert "both be set" in str(e)


def test_prepare_units_ignores_ocr_when_disabled():
    """A failed or disabled OCR must leave the paper on its normal path."""
    import rhapsode
    assert rhapsode._ocr_blocks("/nonexistent.pdf", None) is None
    assert rhapsode._ocr_blocks("/nonexistent.pdf", {"enabled": False}) is None
    # enabled but unreachable endpoint -> None, not an exception
    assert rhapsode._ocr_blocks("/nonexistent.pdf",
                                {"enabled": True,
                                 "modal_endpoint": "http://127.0.0.1:9"}) is None


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
