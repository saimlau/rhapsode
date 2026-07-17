"""Unit tests for the reflow rematch machinery (no LLM, no PDF needed)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reflow


def test_references_cut():
    raw = "Body text here.\nReferences\n[1] Someone. Title.\n"
    assert reflow._references_cut(raw) == raw.index("References")
    # last occurrence wins (an in-text "references" earlier must not cut)
    raw2 = "we discuss references to prior work.\nBody.\nBibliography\n[1] x"
    assert reflow._references_cut(raw2) == raw2.index("Bibliography")
    assert reflow._references_cut("no heading at all") is None


def test_cluster_bands():
    # two words on one line -> one band; a third on another line -> two bands
    rects = [(0, 10, 100, 30, 112), (0, 32, 100, 50, 112), (0, 10, 130, 40, 142)]
    bands = reflow._cluster(rects)
    assert len(bands) == 2
    assert bands[0] == [0, 10, 100, 50, 112]        # merged x-range
    assert bands[1][2] == 130


def test_assign_rects_skips_furniture_and_matches_body():
    # cleaned body is the PDF stream minus interleaved furniture words
    units = [{"kind": "body", "text": "alpha beta gamma", "rects": []}]
    unit_tokens = [["alpha", "beta", "gamma"]]
    stream = [
        (0, 0, 0, 5, 8, "alpha"),
        (1, 9, 9, 9, 9, "footnote"),   # furniture on another page — skipped
        (0, 6, 0, 11, 8, "beta"),
        (0, 12, 0, 17, 8, "gamma"),
    ]
    reflow._assign_rects(units, unit_tokens, stream)
    pages = {r[0] for r in units[0]["rects"]}
    assert pages == {0}                              # furniture page not included
    # all three body words on the same line collapse to one band
    assert units[0]["rects"] == [[0, 0, 0, 17, 8]]


def test_split_sentences():
    s = reflow._split_sentences("First one. Second two! Third three?")
    assert s == ["First one.", "Second two!", "Third three?"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all reflow tests passed")
