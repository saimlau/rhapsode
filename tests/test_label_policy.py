"""Label policy: the model labels, code decides. Unknown labels default to
BODY (never silently lose a paragraph), furniture is dropped, and the
reference list truncates the paper."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fitz
import reflow


def _paper(tmp):
    """A 1-column page: title, body, a caption, and a references section."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "A Study of Things", fontsize=16)
    page.insert_text((72, 200), "The body of the paper explains the method used here.",
                     fontsize=11)
    page.insert_text((72, 300), "Figure 1. A caption that should not be narrated.",
                     fontsize=9)
    page.insert_text((72, 400), "References", fontsize=12)
    page.insert_text((72, 450), "[1] Someone, A journal, 2020.", fontsize=9)
    p = Path(tmp) / "p.pdf"
    doc.save(str(p))
    return str(p)


def _run(pdf, labels, monkeypatch):
    monkeypatch.setattr(reflow.llm, "run",
                        lambda *a, **k: '{"labels": %s, "authors": "", "year": null}'
                        % labels)
    return reflow.extract_document(pdf, {"enabled": True, "runner": "api",
                                         "cache": False})


def test_caption_is_dropped_and_body_kept(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        pdf = _paper(t)
        blocks = reflow._gather_blocks(fitz.open(pdf))
        byid = {b["id"]: " ".join(b["text"].split()) for b in blocks}
        lab = {}
        for i, txt in byid.items():
            lab[str(i)] = (1 if "Study of Things" in txt else
                           4 if txt.startswith("Figure 1") else 0)
        units, meta = _run(pdf, __import__("json").dumps(lab), monkeypatch)
        text = " ".join(u["text"] for u in units)
        assert "body of the paper" in text
        assert "caption that should not" not in text, "CAPTION must be dropped"
        assert meta["title"] == "A Study of Things"


def test_unlabelled_block_defaults_to_body(monkeypatch):
    """The conservative default: a block the model labelled nothing for is
    still narrated, so a forgotten id never silently loses a paragraph."""
    import json
    with tempfile.TemporaryDirectory() as t:
        pdf = _paper(t)
        blocks = reflow._gather_blocks(fitz.open(pdf))
        # label only the title; every other block (incl. the body) is omitted
        lab = {str(b["id"]): 1 for b in blocks
               if "Study of Things" in " ".join(b["text"].split())}
        assert lab, "fixture must contain the title block"
        units, _ = _run(pdf, json.dumps(lab), monkeypatch)
        text = " ".join(u["text"] for u in units)
        assert "body of the paper" in text, "an unlabelled block must be kept"


def test_reference_label_truncates_the_tail(monkeypatch):
    with tempfile.TemporaryDirectory() as t:
        pdf = _paper(t)
        blocks = reflow._gather_blocks(fitz.open(pdf))
        lab = {}
        for b in blocks:
            txt = " ".join(b["text"].split())
            lab[str(b["id"])] = 7 if ("References" in txt or txt.startswith("[1]")) else 0
        units, _ = _run(pdf, __import__("json").dumps(lab), monkeypatch)
        text = " ".join(u["text"] for u in units)
        assert "body of the paper" in text
        assert "Someone" not in text, "the reference list must be cut"


if __name__ == "__main__":
    print("run via pytest (needs monkeypatch)")


def test_total_classification_failure_raises(monkeypatch):
    """A failed LLM must NOT quietly 'succeed' via the BODY default: it has to
    raise so the heuristic extractor takes over. (Regression: the default made
    an all-windows-failed run narrate the whole PDF, furniture and all.)"""
    import llm as llm_mod
    with tempfile.TemporaryDirectory() as t:
        pdf = _paper(t)

        def boom(*a, **k):
            raise llm_mod.LLMError("no runner")
        monkeypatch.setattr(reflow.llm, "run", boom)
        try:
            reflow.extract_document(pdf, {"enabled": True, "runner": "api",
                                          "cache": False})
            assert False, "must raise, not fall through to the BODY default"
        except llm_mod.LLMError:
            pass


def test_empty_label_object_raises(monkeypatch):
    import llm as llm_mod
    with tempfile.TemporaryDirectory() as t:
        pdf = _paper(t)
        monkeypatch.setattr(reflow.llm, "run",
                            lambda *a, **k: '{"labels": {}, "authors": "", "year": null}')
        try:
            reflow.extract_document(pdf, {"enabled": True, "runner": "api",
                                          "cache": False})
            assert False, "an empty labels object is a failure, not a keep-all"
        except llm_mod.LLMError:
            pass


def test_long_prose_mislabelled_furniture_is_rescued(monkeypatch):
    """A 200-word paragraph called FRONTMATTER is a misread, not furniture:
    keep it. (This is what ate a third of the KennedyIII paper.)"""
    import json
    with tempfile.TemporaryDirectory() as t:
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 100), "A Study of Things", fontsize=16)
        long_para = ("The grand challenges of collaborative robotics align well "
                     "with the needs of assistive devices in every respect. ") * 12
        y = 200
        for chunk in [long_para[i:i + 90] for i in range(0, len(long_para), 90)]:
            page.insert_text((72, y), chunk, fontsize=10); y += 12
        p = Path(t) / "p.pdf"; doc.save(str(p))
        blocks = reflow._gather_blocks(fitz.open(str(p)))
        # label the long paragraph as FRONTMATTER (6) — the observed misread
        lab = {}
        for b in blocks:
            txt = " ".join(b["text"].split())
            lab[str(b["id"])] = 1 if "Study of Things" in txt else 6
        units, _ = _run(str(p), json.dumps(lab), monkeypatch)
        text = " ".join(u["text"] for u in units)
        assert "grand challenges" in text, "a long prose block must be rescued"


def test_short_furniture_is_still_dropped(monkeypatch):
    import json
    with tempfile.TemporaryDirectory() as t:
        pdf = _paper(t)
        blocks = reflow._gather_blocks(fitz.open(pdf))
        lab = {}
        for b in blocks:
            txt = " ".join(b["text"].split())
            lab[str(b["id"])] = (1 if "Study of Things" in txt
                                 else 3 if txt.startswith("Figure 1") else 0)
        units, _ = _run(pdf, json.dumps(lab), monkeypatch)
        text = " ".join(u["text"] for u in units)
        assert "caption that should not" not in text, "short furniture stays dropped"
