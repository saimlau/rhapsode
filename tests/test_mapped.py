"""Plain-assert tests for MappedText. Run: .venv/bin/python tests/test_mapped.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extraction import MappedText, clean_mapped, split_sentences

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

print("all MappedText tests passed")
