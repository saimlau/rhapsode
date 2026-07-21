"""The Zotero settings pane is XML parsed at runtime by Zotero, with no build
step and no syntax check — a malformed fragment or an unbound input fails
silently, showing an empty or missing pane to the user. These checks run in
CI-less land: they are the only thing standing between a typo and a broken
settings screen.
"""

import pathlib
import re
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1] / "zotero-plugin"
XUL = "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul"
H = "{http://www.w3.org/1999/xhtml}"


def _tree():
    src = (ROOT / "prefs.xhtml").read_text()
    # Zotero parses the file with parseXULToFragment, which supplies the XUL
    # default namespace and predeclares html: — emulate that exactly
    return ET.fromstring(
        f'<root xmlns="{XUL}" xmlns:html="http://www.w3.org/1999/xhtml">'
        + src + "</root>"), src


def test_fragment_is_well_formed_xml():
    _tree()          # ParseError here is the whole point


def test_html_only_elements_are_namespaced():
    """<label> and <description> are real XUL elements, but <input>/<div>/<h2>
    are not: a bare one parses into a meaningless XUL node and renders
    nothing at all."""
    root, _ = _tree()
    html_only = {"input", "div", "h2", "span", "p", "table"}
    for el in root.iter():
        ns, _, tag = el.tag.partition("}")
        assert not (tag in html_only and ns.lstrip("{") == XUL), \
            f"<{tag}> must be <html:{tag}>"


def test_inputs_are_bound_labelled_and_hinted():
    root, _ = _tree()
    ids = set()
    for el in root.iter(H + "input"):
        assert el.get("preference"), f"{el.get('id')} is bound to no pref"
        assert "." in el.get("preference"), \
            "Zotero warns on a pref key with no dots"
        assert el.get("id"), "an input needs an id to be labelled"
        assert el.get("placeholder"), \
            f"{el.get('id')}: showing the expected format is the point"
        ids.add(el.get("id"))
    assert ids, "the pane has no inputs at all"
    for lab in root.iter(H + "label"):
        assert lab.get("for") in ids, \
            f"<html:label for={lab.get('for')!r}> points at no input"


def test_every_bound_pref_has_a_default():
    """Zotero renders the literal string "undefined" in a field whose pref
    does not exist, so the pane must not bind an undeclared pref."""
    root, src = _tree()
    bound = set(re.findall(r'preference="([^"]+)"', src))
    declared = set(re.findall(r'pref\("([^"]+)"',
                              (ROOT / "build-xpi.sh").read_text()))
    missing = bound - declared
    assert not missing, f"would render 'undefined': {sorted(missing)}"


def test_every_bound_pref_is_read_by_the_plugin():
    """A pane field that writes a pref nothing reads is a dead control."""
    root, src = _tree()
    bs = (ROOT / "bootstrap.js").read_text()
    for key in sorted(set(re.findall(r'preference="([^"]+)"', src))):
        assert f'"{key}"' in bs, f"{key} is bound in the pane, never read"


def test_pane_files_are_packaged_and_id_matches_manifest():
    build = (ROOT / "build-xpi.sh").read_text()
    for name in ("prefs.xhtml", "prefs.css"):
        assert name in build, f"{name} is not added to the XPI"
        assert (ROOT / name).exists(), f"{name} is missing"
    bs = (ROOT / "bootstrap.js").read_text()
    manifest = (ROOT / "manifest.json").read_text()
    plugin_id = re.search(r'"id":\s*"([^"]+)"', manifest).group(1)
    assert f'pluginID: "{plugin_id}"' in bs, \
        "PreferencePanes.register pluginID must equal the manifest id"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all zotero prefs-pane tests passed")
