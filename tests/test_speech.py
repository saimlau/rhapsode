"""Spoken form: a measurement is said in words, while the page keeps its
symbols. Applied at the TTS boundary only, so read-along highlighting (which
comes from the PDF's word boxes) is unaffected."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from speech import for_speech


def test_pressure_and_length_units():
    assert for_speech("yield 276 MPa, modulus 68.9 GPa") == \
        "yield 276 megapascals, modulus 68.9 gigapascals"
    assert for_speech("a 0.5 mm offset") == "a 0.5 millimeters offset"


def test_compound_units_read_as_per():
    assert for_speech("density 2.7 g/cm3") == \
        "density 2.7 grams per cubic centimeter"
    assert for_speech("stiffness 120 N/mm") == "stiffness 120 newtons per millimeter"
    assert for_speech("feed 10 mm/min") == "feed 10 millimeters per minute"


def test_singular_for_one():
    assert for_speech("1 mm") == "1 millimeter"
    assert for_speech("1 s") == "1 second"
    assert for_speech("2 mm") == "2 millimeters"


def test_degrees_and_percent():
    assert for_speech("at 37 °C") == "at 37 degrees Celsius"
    assert for_speech("bent 30°") == "bent 30 degrees"
    assert for_speech("only 5%") == "only 5 percent"


def test_a_unit_without_a_number_is_left_alone():
    """'cm' in prose is a word, not a measurement."""
    t = "The cm and GPa symbols appear in the discussion."
    assert for_speech(t) == t


def test_alloy_designations_are_not_measurements():
    """A digit inside an identifier is not a quantity: Ti6Al4V must never be
    read as '...4 volts'. (It IS spelled out by SAY_AS — see below.)"""
    assert "volts" not in for_speech("Ti6Al4V scaffolds")
    assert for_speech("printed in 3D") == "printed in 3D"


def test_thousands_separator_survives():
    assert for_speech("1,000 mg") == "1,000 milligrams"


def test_empty_and_none_are_safe():
    assert for_speech("") == ""
    assert for_speech(None) is None


def test_display_text_is_untouched_by_the_pipeline():
    """The transform is pure: it never mutates the caller's unit."""
    unit = {"text": "modulus 68.9 GPa"}
    spoken = for_speech(unit["text"])
    assert unit["text"] == "modulus 68.9 GPa", "the unit's own text must not change"
    assert "gigapascals" in spoken


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok", n)


def test_mispronounced_names_are_respelled():
    """Kokoro says Abaqus as 'a-BOK'; a person says AB-uh-kus."""
    assert "Abacus" in for_speech("run in Abaqus")
    assert "Abaqus" not in for_speech("run in Abaqus")


def test_alloy_designations_are_spelled_out_not_measured():
    """Ti-6Al-4V must not become 'Ti-6Al-4 volts' — the hyphen is part of an
    identifier, not a range separator."""
    out = for_speech("Ti-6Al-4V and Ti6Al4V samples")
    assert "volts" not in out
    assert out.count("titanium 6 aluminum 4 vanadium") == 2


def test_a_real_range_still_expands():
    assert for_speech("10-15 mm") == "10-15 millimeters"
    assert for_speech("0.1-0.94 mm") == "0.1-0.94 millimeters"


def test_alphanumeric_grades_are_left_alone():
    assert for_speech("AISI-304L at 276 MPa") == "AISI-304L at 276 megapascals"


def test_initialisms_espeak_says_as_words_are_spelled_out():
    """espeak renders these as words — "eck-um", "eb-um", "hum", "fee",
    "ike-mee", "roy" — so they are spelled out instead."""
    out = for_speech("FEA of the ECM and EBM with HMM; ROI and ICME")
    for want in ["F E A", "E C M", "E B M", "H M M", "R O I", "I C M E"]:
        assert want in out, f"{want} missing from {out!r}"


def test_plural_initialism_wins_over_the_singular():
    """Longest-first plus boundaries: HMM must not fire inside HMMs."""
    assert for_speech("HMMs and HMM") == "H M Ms and H M M"
    assert "M S Cs" in for_speech("the MSCs were counted")


def test_software_names_are_respelled():
    out = for_speech("run in Abaqus, ANSYS and COMSOL")
    assert "Abacus" in out and "Ansis" in out and "Komsol" in out


def test_alloys_and_compounds():
    assert "nickel titanium" in for_speech("a NiTi wire")
    assert "titanium carbide" in for_speech("a TiC coating")


def test_a_term_never_fires_inside_a_longer_token():
    """TiC must not fire inside TiCl4, nor ECM inside ECMO."""
    assert for_speech("TiCl4 and ECMO and HMMx") == "TiCl4 and ECMO and HMMx"


def test_poisson_uses_a_phoneme_override():
    """No RESPELLING works — espeak reads every "pw" as "P-W" ("PEE-wasson") —
    so Kokoro is given the phonemes outright via misaki's markdown override."""
    out = for_speech("the Poisson ratio")
    assert "pw" in out and "/" in out, out
    assert out.startswith("the [Poisson](/") and out.endswith(") ratio"), out


def test_the_phoneme_override_is_honoured_by_the_g2p():
    """The override must reach Kokoro as PHONEMES, not as literal brackets."""
    try:
        from misaki import en
    except Exception:
        return                      # kokoro/misaki not installed in this env
    g2p = en.G2P(trf=False, british=False, fallback=None)
    ps, _ = g2p(for_speech("the Poisson ratio"))
    assert "pw" in ps, f"phonemes not applied: {ps!r}"
    assert "[" not in ps and "/" not in ps, f"brackets leaked into speech: {ps!r}"


def test_both_roi_and_rom_are_spelled_out():
    """Both are overloaded (region of interest / read-only memory), so the
    letters are what a reader wants either way."""
    assert for_speech("ROI and ROM") == "R O I and R O M"


def test_formula_subscripts_are_split_from_symbols():
    """Kokoro reads the join as a word: "Al2O3" came out "AL-too-oh-three"
    and "H2O" "AITCH-too-oh"."""
    assert for_speech("Al2O3") == "aluminum 2 O 3"
    assert for_speech("H2O") == "H 2 O"


def test_symbols_kokoro_cannot_say_use_the_element_name():
    """It has no entry at all for "Mg", and reads "Al" as the name Al.
    Spelling them out fails too — a lone "A" becomes the article."""
    assert for_speech("Mg2") == "magnesium 2"
    assert "aluminum" in for_speech("Al2O3")


def test_formulas_that_already_sounded_right_are_untouched():
    """CO2 is said "C-O two" and TiO2 "T-I-O two" already."""
    assert for_speech("CO2") == "CO2"
    assert for_speech("TiO2") == "TiO2"


def test_part_codes_are_not_broken_by_the_formula_rule():
    """C3D4 (an element type), WE43 (an alloy) and SS316L (a steel) are shaped
    like formulas but already spoken correctly."""
    out = for_speech("C3D4 and WE43 and SS316L")
    assert "WE43" in out and "SS316L" in out
    assert out.startswith("C 3 D 4")      # same sounds as before, spaced


def test_nothing_the_g2p_cannot_pronounce_is_produced():
    """Every expansion must survive Kokoro's own phonemiser."""
    try:
        from misaki import en, espeak
        g2p = en.G2P(trf=False, british=False,
                     fallback=espeak.EspeakFallback(british=False))
    except Exception:
        return
    for t in ["68.9 GPa", "2.7 g/cm3", "Al2O3", "Mg2", "the Poisson ratio",
              "run in Abaqus with NiTi", "ROI and ROM", "37 °C"]:
        ps, _ = g2p(for_speech(t))
        assert "❓" not in ps, f"{t!r} -> unpronounceable {ps!r}"
