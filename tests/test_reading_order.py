"""Reading order is geometry, not the model: columns are gap-split on the left
edges, then read left-to-right, top-to-bottom."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reflow import _columns, _reading_order

W = 612.0


def _b(i, page, x0, y0):
    return {"id": i, "page": page, "x0": x0, "y0": y0, "x1": x0 + 250,
            "y1": y0 + 20, "text": f"b{i}", "words": []}


def test_two_columns_are_not_interleaved():
    # left column x=60, right column x=320; a naive y-sort would zip them
    blocks = [_b(0, 0, 60, 100), _b(1, 0, 320, 100),
              _b(2, 0, 60, 300), _b(3, 0, 320, 300)]
    ids = [b["id"] for b in _reading_order(blocks, {0: W})]
    assert ids == [0, 2, 1, 3], "left column entirely, then right column"


def test_single_column_is_plain_top_down():
    blocks = [_b(0, 0, 70, 500), _b(1, 0, 72, 100), _b(2, 0, 71, 300)]
    assert [b["id"] for b in _reading_order(blocks, {0: W})] == [1, 2, 0]


def test_full_width_title_reads_first():
    # a full-width title shares the left column's x0 and sits highest, so it
    # still comes first without any special case
    blocks = [_b(9, 0, 60, 40), _b(0, 0, 60, 200), _b(1, 0, 320, 200)]
    assert [b["id"] for b in _reading_order(blocks, {0: W})][0] == 9


def test_pages_stay_in_order():
    blocks = [_b(5, 1, 60, 50), _b(0, 0, 60, 700)]
    assert [b["id"] for b in _reading_order(blocks, {0: W, 1: W})] == [0, 5]


def test_columns_gap_split_is_deterministic():
    blocks = [_b(0, 0, 60, 100), _b(1, 0, 320, 100), _b(2, 0, 62, 200)]
    first = _columns(blocks, W)
    for _ in range(5):
        assert _columns(blocks, W) == first, "same input must give same columns"
    assert first[0] == first[2] == 0 and first[1] == 1


def test_three_columns():
    # non-overlapping, as real columns are (the old fixture's 250pt-wide
    # blocks 200pt apart overlapped, which no layout can do)
    blocks = [_wb(0, 0, 40, 100, 220), _wb(1, 0, 240, 100, 420),
              _wb(2, 0, 440, 100, 600)]
    assert [b["id"] for b in _reading_order(blocks, {0: W})] == [0, 1, 2]


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)


def _wb(i, page, x0, y0, x1):
    return {"id": i, "page": page, "x0": x0, "y0": y0, "x1": x1,
            "y1": y0 + 20, "text": f"b{i}", "words": []}


def test_centred_title_reads_before_both_columns():
    """A CENTRED title starts inside the right column's x range; an x0-only
    rule filed it under the right column and read it after the whole left
    column (the Siino regression)."""
    blocks = [
        _wb(11, 0, 161, 75, 450),    # centred title line 1
        _wb(12, 0, 190, 93, 422),    # centred title line 2
        _wb(0, 0, 54, 166, 290),     # left column body
        _wb(5, 0, 302, 165, 545),    # right column body
    ]
    assert [b["id"] for b in _reading_order(blocks, {0: 612.0})] == [11, 12, 0, 5]


def test_spanning_block_splits_bands():
    """A full-width block mid-page separates what precedes it from what
    follows: columns above it are read before it, columns below after."""
    blocks = [
        _wb(0, 0, 54, 100, 290), _wb(1, 0, 302, 100, 545),   # band 0 columns
        _wb(9, 0, 54, 300, 545),                              # full-width bar
        _wb(2, 0, 54, 400, 290), _wb(3, 0, 302, 400, 545),   # band 1 columns
    ]
    assert [b["id"] for b in _reading_order(blocks, {0: 612.0})] == [0, 1, 9, 2, 3]


def test_single_column_paper_is_plain_top_down():
    """Every block is centred/wide, so all are spanning -> plain y order."""
    blocks = [_wb(0, 0, 72, 300, 540), _wb(1, 0, 72, 100, 540),
              _wb(2, 0, 72, 200, 540)]
    assert [b["id"] for b in _reading_order(blocks, {0: 612.0})] == [1, 2, 0]


def test_a_short_centred_fragment_is_not_spanning():
    """A 27pt-wide 'where' at the top of the right column had its centre near
    the page centre, so it was treated as a full-width block and became a band
    boundary — which sorted the whole right column ahead of the left (Regal
    et al. 2025)."""
    blocks = [
        _wb(1, 0, 104, 61, 232),    # left column: '2. SYSTEM DESCRIPTION'
        _wb(2, 0, 43, 79, 260),     # left column body
        _wb(3, 0, 407, 51, 445),    # right column equation
        _wb(4, 0, 305, 61, 332),    # right column 'where' — centred by accident
        _wb(5, 0, 305, 90, 555),    # right column body (fills the column)
        _wb(6, 0, 305, 120, 555),
    ]
    ids = [b["id"] for b in _reading_order(blocks, {0: 595.0})]
    assert ids.index(1) < ids.index(3), \
        f"the left column must be read before the right: {ids}"
    assert ids == [1, 2, 3, 4, 5, 6], ids


def test_a_wide_centred_title_is_still_spanning():
    blocks = [
        _wb(9, 0, 161, 40, 450),    # centred title, 289pt wide on a 612pt page
        _wb(0, 0, 54, 200, 290),
        _wb(1, 0, 302, 200, 545),
    ]
    assert [b["id"] for b in _reading_order(blocks, {0: 612.0})] == [9, 0, 1]
