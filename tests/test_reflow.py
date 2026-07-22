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


def test_narratable_filter():
    assert reflow._narratable("A normal body sentence about the topic.")
    assert reflow._narratable("See Fig. 3.")
    assert not reflow._narratable(" =  x")          # garbled glyphs
    assert not reflow._narratable("1.2 3.4 5.6 7.8 9.0 11.2 13.4")  # table row
    assert reflow._narratable("The modulus was 18 GPa longitudinally.")


def test_sentence_units_continuation():
    # a paragraph split across a break: first token run has no terminal punct
    toks = [("The", [(0, 0, 0, 1, 1)]), ("start", [(0, 2, 0, 3, 1)]),
            ("continues.", [(0, 4, 0, 5, 1)])]
    units = reflow._sentence_units(toks)
    assert len(units) == 1
    assert units[0]["text"] == "The start continues."
    assert units[0]["para_end"] is True



def test_split_sentences_caps_length():
    """A 'sentence' with no terminal punctuation for thousands of chars (a
    thesis appendix, a long list) must still split: the TTS endpoint rejects
    texts over 2000 chars outright."""
    toks = [(f"word{i:04d}", [i]) for i in range(400)]     # ~3600 chars, no '.'
    sents = reflow._split_sentences(toks)
    assert len(sents) > 1, "endless run must be split"
    for sent in sents:
        text = " ".join(t[0] for t in sent)
        assert len(text) <= reflow.SENTENCE_CHAR_LIMIT + 20, len(text)
    # and every token survives, in order
    flat = [t[0] for sent in sents for t in sent]
    assert flat == [t[0] for t in toks]



def test_dropcap_joins_the_word_it_begins():
    """A journal drop cap is its own word, so Arzi et al. opened with
    "R econstruction of large mandibular defects" — narrated exactly that
    way. The cap is set far taller than the text it starts."""
    words = [_w(0, 10, 100, 28, 128, "R"),          # 28 pt tall drop cap
             _w(0, 30, 118, 90, 128, "econstruction"),   # 10 pt body text
             _w(0, 92, 118, 110, 128, "of")]
    toks = reflow._join_dropcap(reflow._tokens(words))
    assert [t[0] for t in toks] == ["Reconstruction", "of"], toks
    assert len(toks[0][1]) == 2, "both boxes kept, so highlighting still works"


def test_ordinary_capital_is_not_glued_to_the_next_word():
    """The guard that matters: "A common approach" must not become
    "Acommon". Same shape as a drop cap, but the same size."""
    words = [_w(0, 10, 118, 20, 128, "A"),
             _w(0, 22, 118, 70, 128, "common"),
             _w(0, 72, 118, 110, 128, "approach")]
    toks = reflow._join_dropcap(reflow._tokens(words))
    assert [t[0] for t in toks] == ["A", "common", "approach"]


def test_dropcap_only_joins_a_lowercase_continuation():
    """A tall capital before another capital is an initial, not a drop cap."""
    words = [_w(0, 10, 100, 28, 128, "R"),
             _w(0, 30, 118, 90, 128, "Smith")]
    toks = reflow._join_dropcap(reflow._tokens(words))
    assert [t[0] for t in toks] == ["R", "Smith"]



class _FakePage:
    def __init__(self, text):
        self._t = text

    def get_text(self, kind="text"):
        return self._t if kind == "text" else []


def test_delivery_cover_sheet_is_recognised():
    """Asking the model to drop it was not enough — one paper narrated three
    minutes of library boilerplate anyway."""
    assert reflow._is_cover_sheet(_FakePage(
        "Thank you for using our service!\nInterlibrary Services\n"
        "The Ohio State University Libraries\nArticle Express documents are "
        "delivered 24/7 directly to your ILLiad account."))
    assert reflow._is_cover_sheet(_FakePage(
        "NOTICE WARNING CONCERNING COPYRIGHT RESTRICTIONS\n"
        "The copyright law of the United States governs photocopying."))


def test_a_paper_that_merely_discusses_copyright_is_kept():
    """The filter needs both the phrasing AND the absence of real prose, or a
    paper about library science would lose its first page."""
    prose = ("Interlibrary loan requests have grown steadily since 2005. " * 90)
    assert len(prose) > 4000
    assert not reflow._is_cover_sheet(_FakePage(prose))
    assert not reflow._is_cover_sheet(_FakePage(
        "A study of mandibular reconstruction plates under cyclic loading."))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all reflow tests passed")
