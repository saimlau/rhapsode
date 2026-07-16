"""GROBID extraction backend for Rhapsode.

GROBID (https://github.com/kermitt2/grobid) is the standard ML pipeline
for scholarly-PDF structure. It runs as a local HTTP service (Docker,
auto-started here); `processFulltextDocument` with sentence segmentation
and coordinates gives us title/authors/sections/sentences with bounding
boxes — a direct match for the read-along sync manifest.
"""

import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

TEI = "{http://www.tei-c.org/ns/1.0}"

_PROC = None       # service Popen when WE started it (dist script only —
                   # never terminate a GROBID someone else owns)
_LAST_USED = 0.0   # last extract() completion, for idle shutdown


def alive(url, timeout=2):
    try:
        return requests.get(f"{url}/api/isalive",
                            timeout=timeout).status_code == 200
    except requests.RequestException:
        return False


def ensure(url, home=None, autostart=True, wait_s=150):
    """True when the service answers, starting the native service from a
    GROBID source install (`home`) if allowed."""
    global _PROC, _LAST_USED
    if alive(url):
        return True
    if not autostart or not home:
        return False
    home = Path(home).expanduser()
    dist = (home / "grobid-service/build/install/grobid-service"
                   "/bin/grobid-service")
    own = False
    if dist.is_file():
        # the dist start script execs java, so the Popen PID IS the JVM
        # and terminate() reaches it — safe to own
        cmd = [str(dist), "server",
               str(home / "grobid-home/config/grobid.yaml")]
        own = True
    elif (home / "gradlew").is_file():
        # gradle runs the JVM under its daemon; terminating the wrapper
        # would orphan the service, so don't claim ownership
        cmd = ["./gradlew", "run", "--quiet"]
    else:
        print(f"warning: no GROBID install at {home}")
        return False
    env = dict(os.environ, GROBID_HOME=str(home / "grobid-home"),
               JAVA_OPTS="-Xmx2g")  # CRF models run fine in 2 GB; an
                                    # uncapped JVM balloons over time
    if (home / "jdk/bin/java").is_file():
        env["JAVA_HOME"] = str(home / "jdk")  # bundled JDK 17 (GROBID
        env["PATH"] = f"{home}/jdk/bin:{env.get('PATH', '')}"  # needs <=17)
    if _PROC is not None and _PROC.poll() is None:
        # our own instance is still starting up — don't spawn a second
        # (which would orphan the first from idle-stop/shutdown)
        pass
    else:
        proc = subprocess.Popen(cmd, cwd=home, env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        if own:
            _PROC = proc
            import atexit  # CLI one-shots must not leave a 2-4 GB JVM behind
            atexit.register(stop)
    print("starting GROBID service (first start loads models)...")
    _LAST_USED = time.time()
    for _ in range(wait_s):
        if alive(url):
            return True
        time.sleep(1)
    return False


def stop():
    """Stop the service if — and only if — this process started it."""
    global _PROC
    if _PROC is None:
        return False
    _PROC.terminate()
    try:
        _PROC.wait(timeout=10)  # reap; a half-dead server must not pass
    except subprocess.TimeoutExpired:  # the next alive() check
        _PROC.kill()
        _PROC.wait()
    _PROC = None
    return True


def touch():
    """The caller just finished paper work — reset the idle clock (the
    worker can't tick during a long synthesis, so extract-time anchoring
    would count synthesis as idle)."""
    global _LAST_USED
    _LAST_USED = time.time()


def maybe_stop(idle_stop_s):
    """Idle shutdown hook for the server's worker loop."""
    if _PROC is not None and time.time() - _LAST_USED >= idle_stop_s:
        if stop():
            print(f"grobid: stopped after {idle_stop_s}s idle")
            return True
    return False


def status():
    return {"owned": _PROC is not None,
            "idle_s": round(time.time() - _LAST_USED, 1) if _LAST_USED else None}


def _coords_to_rects(coords):
    """TEI coords 'page,x,y,w,h;...' -> [[page0, x0, y0, x1, y1], ...]."""
    rects = []
    for part in (coords or "").split(";"):
        vals = part.split(",")
        if len(vals) != 5:
            continue
        try:
            p, x, y, w, h = (float(v) for v in vals)
        except ValueError:
            continue
        rects.append([int(p) - 1, round(x, 2), round(y, 2),
                      round(x + w, 2), round(y + h, 2)])
    return rects


def _text(el):
    """Element text without citation markers and footnote refs."""
    parts = []

    def walk(node):
        if node.tag in (TEI + "ref", TEI + "note"):
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
        if node.tail:
            parts.append(node.tail)

    walk(el)
    text = "".join(parts)
    if el.tail:  # the root element's tail belongs to its parent
        text = text[: len(text) - len(el.tail)]
    return " ".join(text.split())


def _sentences(container, units):
    """Append body units for every <p>/<s> under `container`."""
    for p in container.findall(TEI + "p"):
        added = []
        for s in p.findall(TEI + "s"):
            text = _text(s)
            if len(text) > 1:
                added.append({"kind": "body", "text": text,
                              "rects": _coords_to_rects(s.get("coords")),
                              "para_end": False})
        if added:
            added[-1]["para_end"] = True
            units.extend(added)


def extract(pdf_path, url, timeout=180):
    """Returns (units, meta, warnings): units = {kind, text, rects,
    para_end}; meta = {title, authors, year}. Raises on service errors."""
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{url}/api/processFulltextDocument",
            files={"input": f},
            data={"segmentSentences": "1",
                  "teiCoordinates": ["title", "head", "s"]},
            timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    warnings = []

    meta = {"title": None, "authors": None, "year": None}
    title_el = root.find(f".//{TEI}titleStmt/{TEI}title")
    if title_el is not None and _text(title_el):
        meta["title"] = _text(title_el)
    names = []
    junk = re.compile(r"(?i)sciencedirect|elsevier|springer|ieee|acta|"
                      r"journal|proceedings|conference|universit")
    for pers in root.findall(f".//{TEI}sourceDesc//{TEI}author/{TEI}persName"):
        parts = ([_text(e) for e in pers.findall(TEI + "forename")]
                 + [_text(e) for e in pers.findall(TEI + "surname")])
        name = " ".join(p for p in parts if p)
        words = name.split()
        # GROBID sometimes swallows banner/journal text as an "author"
        if name and not junk.search(name) and len(set(words)) == len(words):
            names.append(name)
    if names:
        meta["authors"] = ", ".join(names)
    date = root.find(f".//{TEI}publicationStmt/{TEI}date")
    for source in ((date.get("when") if date is not None else None),
                   (_text(date) if date is not None else None)):
        m = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", source or "")
        if m:
            meta["year"] = int(m.group())
            break

    units = []
    if meta["title"]:
        units.append({"kind": "heading", "text": meta["title"],
                      "rects": _coords_to_rects(title_el.get("coords")),
                      "para_end": False})
    abstract = root.find(f".//{TEI}profileDesc/{TEI}abstract")
    if abstract is not None:
        ab_units = []
        for div in abstract.findall(f".//{TEI}div") or [abstract]:
            _sentences(div, ab_units)
        if ab_units:  # the TEI <abstract> element exists even when empty
            units.append({"kind": "heading", "text": "Abstract.",
                          "rects": [], "para_end": False})
            units.extend(ab_units)

    body = root.find(f".//{TEI}text/{TEI}body")
    if body is not None:
        for div in body.findall(TEI + "div"):
            head = div.find(TEI + "head")
            if head is not None and _text(head):
                n = head.get("n", "")
                text = (f"{n} {_text(head)}".strip() if n else _text(head))
                # figure labels / reference fragments occasionally parse as heads
                if (re.search(r"[A-Za-z]{3}", text)
                        and not re.match(r"(?i)^(figure|fig\b|table)", text)):
                    units.append({"kind": "heading", "text": text,
                                  "rects": _coords_to_rects(head.get("coords")),
                                  "para_end": False})
            _sentences(div, units)

    global _LAST_USED
    _LAST_USED = time.time()
    if sum(len(u["text"]) for u in units) < 500:
        raise ValueError("GROBID returned almost no text")
    recovered = _recover_missing_headings(pdf_path, units)
    if recovered:
        warnings.append(f"recovered {recovered} section heading(s) GROBID "
                        f"missed")
    covered = sum(1 for u in units if u["rects"])
    if covered < 0.8 * len(units):
        warnings.append(f"GROBID coordinates missing for "
                        f"{len(units) - covered}/{len(units)} units")
    return units, meta, warnings


HEADING_LINE = re.compile(
    r"(?:[IVX]{1,4}|[A-Z]|\d{1,2})[.)]\s+[A-Z][A-Z\d\s&,'’:-]{3,}")


def _recover_missing_headings(pdf_path, units):
    """GROBID sometimes drops run-in section headings entirely (they appear
    neither as <head> nor in any <p>). Recover heading-shaped ALL-CAPS lines
    from the PDF and insert them at their coordinate position."""
    import fitz
    doc = fitz.open(pdf_path)
    mids = [p.rect.width / 2 for p in doc]

    def key(page, x0, y0):
        return (page, 0 if x0 < mids[page] else 1, y0)

    have = {re.sub(r"[^a-z0-9]", "", u["text"].lower())
            for u in units if u["kind"] == "heading"}
    found = []
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                text = " ".join("".join(
                    s["text"] for s in line["spans"]).split())
                letters = [c for c in text if c.isalpha()]
                if not (6 <= len(text) <= 90) or not letters:
                    continue
                if sum(c.isupper() for c in letters) / len(letters) < 0.85:
                    continue
                for m in HEADING_LINE.finditer(text):
                    cand = m.group().strip(" .,:-")
                    norm = re.sub(r"[^a-z0-9]", "", cand.lower())
                    if (len(norm) < 6
                            or re.match(r"(?i)^(figure|fig|table)\b", cand)
                            or any(norm in h or h in norm for h in have)):
                        continue
                    b = line["bbox"]
                    found.append({"kind": "heading", "text": cand,
                                  "rects": [[page.number, round(b[0], 2),
                                             round(b[1], 2), round(b[2], 2),
                                             round(b[3], 2)]],
                                  "para_end": False})
                    have.add(norm)
    for h in found:
        r = h["rects"][0]
        hkey = key(r[0], r[1], r[2])
        idx = len(units)
        for i, u in enumerate(units):
            ur = u["rects"][0] if u["rects"] else None
            if ur and key(ur[0], ur[1], ur[2]) > hkey:
                idx = i
                break
        units.insert(idx, h)
    return len(found)
