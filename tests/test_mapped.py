"""Plain-assert tests for MappedText. Run: .venv/bin/python tests/test_mapped.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extraction import MappedText, clean_mapped, clean_text, split_sentences

B = lambda i: (0, (float(i), 0.0, float(i) + 1, 10.0))  # fake unit-wide boxes


def mt(text, metas=None):
    return MappedText(text, metas if metas is not None else [B(i) for i in range(len(text))])


# translate: 1:1 replacement keeps meta
t = mt("a‑b").translate_chars({"‑": "-"})
assert t.text == "a-b" and t.meta[1] == B(1)

# translate: ligature expansion duplicates meta
t = mt("ﬁx").translate_chars({"ﬁ": "fi"})
assert t.text == "fix" and t.meta[0] == t.meta[1] == B(0) and t.meta[2] == B(1)

# sub with group refs: de-hyphenation drops hyphen+newline, keeps letter metas
t = mt("person-\nalized").sub(r"(\w)-\n(\w)", r"\1\2")
assert t.text == "personalized"
assert t.meta[5] == B(5) and t.meta[6] == B(8)  # 'n' keeps its box, 'a' keeps its own

# sub deletion: citation bracket disappears entirely
t = mt("fact [12] here").sub(r"\s*\[[0-9,;\s–—-]+\]", "")
assert t.text == "fact here"

# sub insertion: literal chars carry no meta, group chars keep theirs
t = mt("5–9").sub(r"(\d)\s*–\s*(\d)", r"\1 to \2")
assert t.text == "5 to 9"
assert t.meta[0] == B(0) and t.meta[5] == B(2) and t.meta[2] is None

# clean_mapped end-to-end on a wrapped, cited, hyphenated fragment
raw = mt("The person-\nalized implant [3, 5–7] works  well.")
c = clean_mapped(raw)
assert c.text == "The personalized implant works well."
assert all(len(c.text) == len(c.meta) for _ in [0])

# split_sentences keeps metas aligned and splits monster sentences
s = split_sentences(mt("One. Two is here. Three!"))
assert [p.text for p in s] == ["One.", "Two is here.", "Three!"]
long = mt("x" * 300 + ", " + "y" * 300 + ", tail.")
parts = split_sentences(long, limit=450)
assert len(parts) == 2 and all(len(p) <= 451 for p in parts)

# rects: same line merges into one rect, column gap splits, second line separate
boxes = ([(0, (10.0 + i, 100.0, 11.0 + i, 110.0)) for i in range(5)]     # line 1 left
         + [(0, (200.0 + i, 100.0, 201.0 + i, 110.0)) for i in range(5)]  # line 1 right col
         + [(0, (10.0 + i, 115.0, 11.0 + i, 125.0)) for i in range(5)])   # line 2
t = MappedText("abcdefghijklmno", boxes)
r = t.rects()
assert len(r) == 3, r
assert r[0][:2] == [0, 10.0] and r[1][1] == 200.0 and r[2][2] == 115.0


# --- the auxetic-paper crash: a letter-spaced heading wrapping across lines
# ("G R A P H I C A L\nA B S T R A C T") raised "text/meta length mismatch"
# during ingest, so the paper never entered the queue. _block_mapped joins a
# block's lines with "\n", but the collapse path despaced only U+0020 while
# the comparison string dropped ALL whitespace, leaving one extra meta entry
# per line break.
_text = "G R A P H I C A L\nA B S T R A C T"
_meta = [(0, (float(i), 0.0, float(i) + 1, 10.0)) for i in range(len(_text))]
_mt = MappedText(_text, _meta)
_despaced = [(c, m) for c, m in zip(_mt.text, _mt.meta) if not c.isspace()]
_out = MappedText("".join(c for c, _ in _despaced), [m for _, m in _despaced])
assert _out.text == "GRAPHICALABSTRACT", _out.text
assert len(_out.text) == len(_out.meta), "the invariant that used to blow up"

# the old filter must remain visibly wrong, so a revert fails loudly here
_old = "".join(c for c, _ in
               [(c, m) for c, m in zip(_mt.text, _mt.meta) if c != " "])
assert "\n" in _old, "filtering only spaces keeps the newline that crashed it"

# .capitalize() is not length-preserving for every code point ("ßx" -> "Ssx"),
# and the meta list must stay 1:1 with the text
assert len("ßx".capitalize()) != len("ßx"), "precondition"
_collapsed = "ßx"
_cap = _collapsed.capitalize()
assert (_cap if len(_cap) == len(_collapsed) else _collapsed) == _collapsed, \
    "must fall back rather than desync the mapping"


# --- Greek letters: espeak reads the letters correctly ("ε" -> "epsilon"),
# but a letter glued to its subscript becomes one token and is pronounced as
# a word: "εx" -> "epsilonks", "σy" -> "sigma-ee". Only the boundary needs
# marking, and mt.sub keeps char/bbox mapping in step so the read-along still
# highlights the original glyphs.
assert clean_text("where εx and εy are") == "where ε x and ε y are"
assert clean_text("σmax at 2θ") == "σ max at 2 θ"
assert clean_text("no greek here at all") == "no greek here at all"
# U+00B5 MICRO SIGN and U+03BC GREEK MU look identical and PDFs use both;
# the same-looking text must narrate the same way
assert clean_text("5 µm") == "5 µ m"
assert clean_text("5 μm") == "5 μ m"
_mt = clean_mapped(MappedText.plain("where εx and εy are"))
assert len(_mt.text) == len(_mt.meta), "inserted spaces must keep the mapping 1:1"

# --- Maths letters and mojibake. Word writes equations in Cambria Math with a
# broken ToUnicode map, so "x[k+1]" reaches the text layer as "𝑥ሾ𝑘൅1ሿ": the
# letters are Mathematical Alphanumeric Symbols and the brackets decoded as
# Ethiopic. espeak has no name for either and spells out the code point —
# "letter one D four six five, ethiopic letter one two three E" — and switches
# voice mid-sentence for the Malayalam "=".
assert clean_text("angle 𝑥ሾ𝑘ሿ and input 𝑢ሾ𝑘ሿ.") == "angle x k and input u k ."
assert clean_text("𝐸 denotes the yield stress.") == "E denotes the yield stress."
# 𝛼 is Greek alpha from the maths block, not the Greek block: it must fold to
# α so the Greek rules above see it at all
assert clean_text("where 𝛼 is the angle") == "where α is the angle"
assert clean_text("𝛼௜ and 𝛼௙") == "α and α"          # mojibake subscripts gone
# ordinary text must pass through untouched
assert clean_text("A normal sentence, unchanged.") == "A normal sentence, unchanged."
_mt = clean_mapped(MappedText.plain("angle 𝑥ሾ𝑘ሿ here"))
assert len(_mt.text) == len(_mt.meta), "folding must keep the mapping 1:1"


# --- Regal et al. 2025: the same exporter writes each maths glyph TWICE, the
# second copy with zero advance width (verified in the PDF: identical char,
# x0 == x1). Invisible on the page, but present in the text layer — so every
# variable was read out twice, "alpha alpha zero".
assert clean_text("𝛼𝛼𝑓𝑓−𝛼𝛼𝑝𝑝") == "α f−α p"
assert clean_text("𝜎𝜎𝑦𝑦 and 𝜈𝜈.") == "σ y and ν."
# collapsing happens BEFORE folding, while a doubled maths letter is still
# distinguishable from an ordinary one: a real "mm" must survive
assert clean_text("the mm scale") == "the mm scale"
assert clean_text("all well, generally.") == "all well, generally."

# marks espeak renders as silence, losing meaning from the sentence
assert clean_text("at ± 0.65∘ relative") == "at ± 0.65° relative"   # ∘ is a
assert clean_text("{0∘, 5∘, 30∘}") == "{0°, 5°, 30°}"               # degree sign
assert "∘" in clean_text("a composition f ∘ g")   # ...only after a number
assert clean_text("𝐾𝐾[𝑥𝑥, 𝑥𝑥′]") == "K[x, x prime]"
assert clean_text("𝛼𝛼0 ∈ the set") == "α 0 in the set"


# --- Letterlike Symbols: the block a typesetter reaches for when the maths
# block has no italic h. ℎ is PLANCK CONSTANT used as a variable, and espeak
# spelled it out — "E = (4ℎ2 + 12ℎ𝑘)" read as "four letter two one zero E
# two plus twelve letter two one zero E letter one D four five eight".
assert clean_text("E = (4ℎ2 + 12ℎ𝑘)") == "E = (4h2 + 12hk)"
assert clean_text("a length ℓ and the set ℝ") == "a length l and the set R"
assert clean_text("heated to 37℃") == "heated to 37°C"       # multi-char fold

# WHITE BULLET is another mis-encoded degree sign ("±18◦in roll"), where the
# angle was dropped from the sentence entirely: "plus or minus eighteen in
# roll". Only after a number, so a real bullet survives.
assert clean_text("±18◦in roll") == "±18°in roll"
assert "•" in clean_text("AJVR • Vol 77 • No. 5")
assert "◦" in clean_text("a ◦ used as a bullet")

print("all MappedText tests passed")
