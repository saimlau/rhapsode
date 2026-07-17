"""Unit tests for the block-classification reflow helpers (no LLM/PDF)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reflow


def _w(page, x0, y0, x1, y1, text):
    return (page, x0, y0, x1, y1, text)


def test_tokens_dehyphenate():
    words = [_w(0, 0, 0, 10, 8, "re-"), _w(0, 0, 10, 40, 18, "placement"),
             _w(0, 42, 10, 60, 18, "now.")]
    toks = reflow._tokens(words)
    assert [t[0] for t in toks] == ["replacement", "now."]
    # the de-hyphenated token keeps both fragment boxes for highlighting
    assert len(toks[0][1]) == 2


def test_split_sentences_and_abbrev():
    # "Fig." must not end a sentence; the real "." must
    toks = [("See", [1]), ("Fig.", [1]), ("3", [1]), ("here.", [1]),
            ("Next", [1]), ("one.", [1])]
    sents = reflow._split_sentences(toks)
    assert len(sents) == 2
    assert [t[0] for t in sents[0]] == ["See", "Fig.", "3", "here."]


def test_cluster_bands():
    boxes = [(0, 10, 100, 30, 112), (0, 32, 100, 50, 112), (0, 10, 130, 40, 142)]
    bands = reflow._cluster(boxes)
    assert len(bands) == 2
    assert bands[0] == [0, 10, 100, 50, 112]


def test_headtail():
    head, tail = reflow._headtail("First sentence here. Middle. Last one.")
    assert head == "First sentence here."
    assert tail == "Last one."


def test_parse_decision():
    d = reflow._parse_decision('noise {"order":[1,2],"headings":[1]} trailing')
    assert d["order"] == [1, 2] and d["headings"] == [1]


def test_sentence_units_continuation():
    # a paragraph split across a break: first token run has no terminal punct
    toks = [("The", [(0, 0, 0, 1, 1)]), ("start", [(0, 2, 0, 3, 1)]),
            ("continues.", [(0, 4, 0, 5, 1)])]
    units = reflow._sentence_units(toks)
    assert len(units) == 1
    assert units[0]["text"] == "The start continues."
    assert units[0]["para_end"] is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all reflow tests passed")
