"""A block boundary that falls mid-word is not a word boundary. Two cases the
PDF creates and _tokens cannot see (it only ever looks inside one block):
a drop cap emitted as its own block, and a word hyphenated across the seam."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reflow


def _tok(text, x0=0, y0=0, x1=10, y1=10):
    return (text, [(0, x0, y0, x1, y1)])


def test_dropcap_joins_a_small_caps_continuation():
    """"A" + "S ROBOT manipulators" is "AS ROBOT ..." — the rest of the word is
    set in small caps, not lower case."""
    toks = [_tok("A", y0=0, y1=30), _tok("S", y0=0, y1=10), _tok("ROBOT")]
    out = reflow._join_dropcap(toks)
    assert out[0][0] == "AS", f"expected AS, got {out[0][0]!r}"
    assert out[1][0] == "ROBOT"


def test_dropcap_still_joins_a_lowercase_continuation():
    toks = [_tok("R", y0=0, y1=30), _tok("econstruction", y0=0, y1=10)]
    assert reflow._join_dropcap(toks)[0][0] == "Reconstruction"


def test_a_real_article_a_is_not_glued_to_the_next_word():
    """"A" + "FULLY autonomous robot" must stay two words."""
    toks = [_tok("A", y0=0, y1=30), _tok("FULLY", y0=0, y1=10), _tok("autonomous")]
    out = reflow._join_dropcap(toks)
    assert out[0][0] == "A" and out[1][0] == "FULLY"


def test_lone_capital_I_is_not_treated_as_a_fragment():
    toks = [_tok("A", y0=0, y1=30), _tok("I", y0=0, y1=10)]
    assert reflow._join_dropcap(toks)[0][0] == "A"


def test_a_short_capital_is_only_joined_under_a_tall_cap():
    """Without the drop-cap height ratio there is no join at all."""
    toks = [_tok("A", y0=0, y1=10), _tok("S", y0=0, y1=10)]
    assert reflow._join_dropcap(toks)[0][0] == "A"


def test_hyphen_weld_across_a_block_seam():
    """"...be-" ending one block and "tween..." starting the next is one word.
    Exercised through extract_document, which owns the seam."""
    import fitz, tempfile, json
    from pathlib import Path
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "The clearance be-", fontsize=10)
        page.insert_text((72, 300), "tween the screw and the bone is superior.",
                         fontsize=10)      # far below: a separate block
        p = Path(t) / "hyphen.pdf"; doc.save(str(p))

        blocks = reflow._gather_blocks(fitz.open(str(p)))
        assert len(blocks) == 2, f"fixture must give two blocks: {len(blocks)}"
        labels = json.dumps({str(b["id"]): 0 for b in blocks})   # both BODY
        import types
        orig = reflow.llm.run
        reflow.llm.run = lambda *a, **k: '{"labels": %s, "authors": "", "year": null}' % labels
        try:
            units, _ = reflow.extract_document(str(p), {"enabled": True,
                                                        "runner": "api", "cache": False})
        finally:
            reflow.llm.run = orig
        text = " ".join(u["text"] for u in units)
        assert "between" in text, f"the seam must weld into one word: {text!r}"
        assert "be tween" not in text


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)
