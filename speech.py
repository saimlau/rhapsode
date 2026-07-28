"""Spoken form of a unit's text.

The page shows "68.9 GPa"; a narrator says "68.9 gigapascals". Kokoro reads an
unexpanded symbol letter by letter ("G P A"), which is unintelligible in a
methods section full of them.

This runs at the TTS boundary ONLY. The unit's own text is left untouched, so
the read-along still displays and highlights exactly what the PDF says — the
rectangles come from the PDF's word boxes, not from the spoken string, so the
two can differ safely.

Deliberately conservative: a unit is expanded only where it follows a NUMBER,
which is what makes it a measurement rather than a word. "5 cm" becomes
centimeters; "cm" alone, or "In" in a sentence, is left alone.
"""

import re

# symbol -> (singular, plural). Order matters only for the compound forms
# below, which are matched first.
UNITS = {
    "nm": ("nanometer", "nanometers"),
    "µm": ("micrometer", "micrometers"), "um": ("micrometer", "micrometers"),
    "mm": ("millimeter", "millimeters"),
    "cm": ("centimeter", "centimeters"),
    "km": ("kilometer", "kilometers"),
    "m": ("meter", "meters"),
    "mg": ("milligram", "milligrams"),
    "kg": ("kilogram", "kilograms"),
    "g": ("gram", "grams"),
    "GPa": ("gigapascal", "gigapascals"),
    "MPa": ("megapascal", "megapascals"),
    "kPa": ("kilopascal", "kilopascals"),
    "Pa": ("pascal", "pascals"),
    "kN": ("kilonewton", "kilonewtons"),
    "mN": ("millinewton", "millinewtons"),
    "N": ("newton", "newtons"),
    "GHz": ("gigahertz", "gigahertz"), "MHz": ("megahertz", "megahertz"),
    "kHz": ("kilohertz", "kilohertz"), "Hz": ("hertz", "hertz"),
    "ms": ("millisecond", "milliseconds"),
    "µs": ("microsecond", "microseconds"),
    "s": ("second", "seconds"),
    "min": ("minute", "minutes"),
    "h": ("hour", "hours"),
    "kV": ("kilovolt", "kilovolts"), "mV": ("millivolt", "millivolts"),
    "V": ("volt", "volts"),
    "kW": ("kilowatt", "kilowatts"), "mW": ("milliwatt", "milliwatts"),
    "W": ("watt", "watts"),
    "kJ": ("kilojoule", "kilojoules"), "J": ("joule", "joules"),
    "mol": ("mole", "moles"),
    "mL": ("milliliter", "milliliters"), "ml": ("milliliter", "milliliters"),
    "L": ("liter", "liters"),
    "wt%": ("weight percent", "weight percent"),
    "%": ("percent", "percent"),
    "°C": ("degree Celsius", "degrees Celsius"),
    "°F": ("degree Fahrenheit", "degrees Fahrenheit"),
    "°": ("degree", "degrees"),
    "rpm": ("revolution per minute", "revolutions per minute"),
}

# exponents read as words, so "cm3" is "cubic centimeters", "mm2" is "square
# millimeters" — and a bare "N/mm2" does not come out "n slash m m two"
_POWER = {"2": "square ", "3": "cubic ", "²": "square ", "³": "cubic "}

_SYMS = sorted(UNITS, key=len, reverse=True)      # longest first: GPa before Pa
_SYM_RE = "|".join(re.escape(s) for s in _SYMS)
_POW_RE = "[23²³]?"
# a number (1, 2.7, 1,000, 5-10, ±0.65) immediately before a unit, optionally
# a compound one ("g/cm3", "N/mm", "mm/min")
_MEASURE = re.compile(
    # a letter immediately before the number means it is part of an identifier
    # ("Ti6Al4V"), not a measurement
    r"(?<![A-Za-z])"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?P<sp>[\s ]*)"
    r"(?P<unit>(?:%s)%s)"
    r"(?:(?P<slash>/)(?P<den>(?:%s)%s))?"
    r"(?![A-Za-z0-9])" % (_SYM_RE, _POW_RE, _SYM_RE, _POW_RE))


# Proper nouns and alloy designations a general-purpose TTS mangles. Respelled
# for the SPOKEN string only — the page still shows the real word. Keep this
# short and certain: a wrong respelling is worse than an odd one, so add a term
# only when you know how it is actually said.
SAY_AS = {
    "Abaqus": "Abacus",                 # AB-uh-kus, not "a-BOK"
    "Ti6Al4V": "titanium 6 aluminum 4 vanadium",
    "Ti-6Al-4V": "titanium 6 aluminum 4 vanadium",
    "TiAl6V4": "titanium aluminum 6 vanadium 4",
    "CoCrMo": "cobalt chrome molybdenum",
    "PEEK": "peek",                     # the polymer, said as the word
    "in vivo": "in veevo",
    "in vitro": "in veetro",
}
_SAY_AS_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(SAY_AS, key=len, reverse=True)))


def _respell(text):
    """Say the words a TTS gets wrong the way a person would."""
    return _SAY_AS_RE.sub(lambda m: SAY_AS[m.group(0)], text)


def _say(symbol, plural):
    """A unit symbol, possibly with an exponent, as words."""
    power = ""
    if symbol and symbol[-1] in _POWER:
        power, symbol = _POWER[symbol[-1]], symbol[:-1]
    pair = UNITS.get(symbol)
    if not pair:
        return None
    return power + (pair[1] if plural else pair[0])


def for_speech(text):
    """`text` as it should be SPOKEN: measurements expanded to words."""
    if not text:
        return text

    def sub(m):
        # A designation like "Ti-6Al-4V" or "AISI-304L" is not a measurement.
        # A letter anywhere earlier in the SAME whitespace-delimited token says
        # so — the hyphen alone does not, since "10-15 mm" is a real range.
        head = m.string[:m.start()]
        tok = m.string[max(head.rfind(" "), head.rfind("\n"),
                           head.rfind("\t")) + 1:m.start()]
        if re.search(r"[A-Za-z]", tok):
            return m.group(0)
        num = m.group("num")
        try:
            plural = float(num.replace(",", "")) != 1
        except ValueError:
            plural = True
        said = _say(m.group("unit"), plural)
        if said is None:
            return m.group(0)
        out = f"{num} {said}"
        den = m.group("den")
        if den:
            # "per" reads the solidus: 2.7 g/cm3 -> grams per cubic centimeter
            tail = _say(den, False)
            if tail is None:
                return m.group(0)
            out += f" per {tail}"
        return out

    # units FIRST: expanding "Ti6Al4V" beforehand would leave "4 V", which the
    # measurement rule would then read as "4 volts"
    return _respell(_MEASURE.sub(sub, text))
