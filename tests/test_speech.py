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


def test_poisson_is_deliberately_left_alone():
    """espeak splits every 'pw' spelling into 'P-W' ("PEE-wasson"), which is
    worse than its plain "POY-son" — so it is not in the table."""
    assert for_speech("the Poisson ratio") == "the Poisson ratio"
