"""Find US-only units in recipe text and propose rounded metric replacements.

Cups, tablespoons and teaspoons are left alone on purpose. Only pounds,
ounces, gallons/quarts/pints, inches and Fahrenheit are converted, and the
result is rounded to a number you would actually weigh or measure
(1 lb -> 450 g, not 453.59 g).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

_VULGAR = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
_V = "".join(_VULGAR)

# One number: "1", "1.5", "1/2", "1 1/2", "1½", "½"
_NUM = rf"(?:\d+[\s-]\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?\s?[{_V}]|\d+(?:[.,]\d+)?|[{_V}])"
# Optional range or dimensions: "1-2", "1 to 2", "9x13", "9 by 13"
_QTY = rf"(?P<a>{_NUM})(?:\s*(?P<sep>-|–|to|x|×|by)\s*(?P<b>{_NUM}))?"

_UNITS = {
    "lb": r"pounds?|lbs?\.?",
    "floz": r"fl\.?\s?oz\.?|fluid\s+ounces?",
    "oz": r"ounces?|oz\.?",
    "gal": r"gallons?|gal\.?",
    "qt": r"quarts?|qts?\.?",
    "pt": r"pints?",
    "in": r"inch(?:es)?",
    "F": r"[°º]\s?F\b|degrees\s+F(?:ahrenheit)?\b|F(?:ahrenheit)\b",
}
_UNIT_ALT = "|".join(f"(?P<{k}>{v})" for k, v in _UNITS.items())
PATTERN = re.compile(rf"(?<![\w/.,]){_QTY}(?P<gap>\s?-?\s?)(?:{_UNIT_ALT})(?![a-zA-Z])", re.I)

LABELS = {"lb": "pounds", "oz": "ounces", "floz": "fluid ounces", "gal": "gallons",
          "qt": "quarts", "pt": "pints", "in": "inches", "F": "Fahrenheit"}

_LIQUID = re.compile(
    r"\b(milk|water|stock|broth|cream(?!\s+cheese)|juice|wine|beer|oil|vinegar|sauce|buttermilk|coffee|syrup|"
    r"milch|wasser|brühe|sahne|saft|wein|öl)\b", re.I)


@dataclass(frozen=True)
class Hit:
    unit: str          # key of LABELS
    original: str      # matched text, e.g. "1 1/2 pounds"
    proposed: str      # e.g. "675 g"
    liquid: bool = False   # ounces of a liquid, converted as volume (ml)


def _num(text: str) -> float:
    for k, v in _VULGAR.items():
        text = text.replace(k, " " + v)
    text = re.sub(r"(\d)-(\d+/)", r"\1 \2", text)  # "2-1/2" is two and a half
    return float(sum(Fraction(p) for p in text.replace(",", ".").split()))


def _fmt(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _round_to(x: float, step: float) -> float:
    return max(step, round(x / step) * step)


def _grams(g: float) -> tuple[float, str]:
    if g >= 1000:
        return round(g / 1000, 1), "kg"
    return _round_to(g, 5 if g < 100 else 10 if g < 200 else 25), "g"


def _ml(ml: float) -> tuple[float, str]:
    if ml >= 1000:
        return round(ml / 1000, 1), "l"
    return _round_to(ml, 5 if ml < 100 else 25), "ml"


def _cm(cm: float) -> tuple[float, str]:
    return (_round_to(cm, 0.5) if cm < 5 else round(cm)), "cm"


def _celsius(f: float) -> tuple[float, str]:
    return _round_to((f - 32) * 5 / 9, 10 if f >= 250 else 1), "°C"


_CONVERT = {
    "lb": lambda v: _grams(v * 453.6),
    "oz": lambda v: _grams(v * 28.35),
    "floz": lambda v: _ml(v * 29.57),
    "gal": lambda v: _ml(v * 3785),
    "qt": lambda v: _ml(v * 946),
    "pt": lambda v: _ml(v * 473),
    "in": lambda v: _cm(v * 2.54),
    "F": lambda v: _celsius(v),
}


def _convert(m: re.Match, line: str) -> Hit:
    unit = next(k for k in _UNITS if m.group(k))
    liquid = unit == "oz" and bool(_LIQUID.search(line))
    conv = _CONVERT["floz" if liquid else unit]
    values = [conv(_num(m.group(g))) for g in ("a", "b") if m.group(g)]
    # Use one unit for both ends of a range: "2-3 lb" -> "0.9–1.4 kg".
    if len(values) == 2 and values[0][1] != values[1][1]:
        values = [(round(v / 1000, 1), values[1][1]) if u != values[1][1] else (v, u) for v, u in values]
    sep = m.group("sep") or ""
    sep = {"x": " x ", "×": " x ", "by": " x ", "to": " to "}.get(sep.lower(), "–" if sep else "")
    out_unit = values[-1][1]
    space = "" if out_unit == "°C" else " "
    return Hit(unit, m.group(0), sep.join(_fmt(v) for v, _ in values) + space + out_unit, liquid)


def find_us_units(line: str) -> list[Hit]:
    return [_convert(m, line) for m in PATTERN.finditer(line)]


# A measurement that is already metric, e.g. "450g", "~1.25 kg", "2.5 cm", "180°C".
_METRIC = (rf"~?{_NUM}(?:\s*(?:-|–|to|x|×)\s*{_NUM})?\s?"
           r"(?:kg|grams?|gr|g|ml|cl|l|cm|mm|[°º]\s?C|degrees\s+C(?:elsius)?)\b")
_LOOKBEHIND = r"(?<![\w/.,])"
_US_BARE = re.sub(r"\?P<\w+>", "?:", PATTERN.pattern).replace(_LOOKBEHIND, "", 1)
_APPROX = r"(?:~|about\s+|approx\.?\s+|roughly\s+)?"
# One US amount, or a compound one: "2lb 11 oz", "1 pound plus 5 ounces".
_US_AFTER = rf"{_APPROX}{_US_BARE}(?:\s+(?:plus\s+)?{_US_BARE})?"
_US = _LOOKBEHIND + _US_AFTER
_M = rf"{_APPROX}(?P<m>{_METRIC})"
# Lines that give both systems: keep the author's metric number, drop the US one.
_DUAL = [
    (re.compile(rf"{_US}\s*\(\s*{_M}\s*\)", re.I), r"\g<m>"),                 # 1 lb (450g)
    (re.compile(rf"{_US}\s*\(\s*{_M}\s*[;/]\s*", re.I), r"\g<m> ("),          # 8.8 oz (250g/2 balls)
    (re.compile(rf"(?P<m>{_METRIC})\s*\(\s*{_US_AFTER}\s*\)", re.I), r"\g<m>"),  # 450 g (1 lb)
    (re.compile(rf"{_US}\s*/\s*{_M}", re.I), r"\g<m>"),                       # 2 lb / 1 kg
    (re.compile(rf"(?P<m>{_METRIC})\s*/\s*{_US_AFTER}", re.I), r"\g<m>"),     # 400ml / 14 oz
    (re.compile(rf"(?<=\(){_US_AFTER}\s*(?:,|or)\s*{_M}(?=[\s,;)])", re.I), r"\g<m>"),          # (4 oz, 120 g)
    (re.compile(rf"(?<=\()(?P<m>{_METRIC})\s*(?:,|or)\s*{_US_AFTER}(?=[\s,;)])", re.I), r"\g<m>"),  # (120 g, 4 oz)
    (re.compile(rf"{_US}\s+(?P<m>{_METRIC})", re.I), r"\g<m>"),               # 8.8 oz 250g
    (re.compile(rf"(?P<m>{_METRIC}[^\d]*?)\s*[,–-]\s*about\s+{_US_BARE}\s*$", re.I), r"\g<m>"),  # 200 g pork, about 7oz
    (re.compile(rf"(?P<m>{_METRIC}[^()\d]*?)\s*\(\s*{_US_AFTER}\s*\)\s*$", re.I), r"\g<m>"),  # 400g minced pork (14 oz)
]
# "14 oz ricotta cheese (400g)" / "8.8 oz mozzarella (2 balls / 250g)": metric at the end of the line.
_TRAILING = re.compile(
    rf"^(?P<pre>\s*(?:[-*]\s+)?){_US}(?P<mid>[^()]*?)\s*\((?:(?P<alt>[^()/]+?)\s*/\s*)?(?P<m>{_METRIC})\)(?P<end>\s*)$", re.I)
_HAS_METRIC = re.compile(_METRIC, re.I)
_HAS_US = re.compile(_US_BARE, re.I)


def _drop_us_duplicates(line: str) -> str:
    m = _TRAILING.match(line)
    if m and not _HAS_US.search(m.group("mid")) and not _HAS_METRIC.search(m.group("mid")):
        alt = f" ({m.group('alt')})" if m.group("alt") else ""
        line = f"{m.group('pre')}{m.group('m')}{m.group('mid')}{alt}{m.group('end')}"
    for pat, repl in _DUAL:
        line = pat.sub(repl, line)
    # "250g ground beef (mince) (250g)": the same amount twice.
    tail = re.search(rf"\s*\((?P<m>{_METRIC})\)(?P<end>\s*)$", line, re.I)
    if tail:
        key = re.sub(r"\s", "", tail.group("m")).casefold()
        head = line[: tail.start()]
        if any(re.sub(r"\s", "", x.group(0)).casefold() == key for x in _HAS_METRIC.finditer(head)):
            line = head + tail.group("end")
    return line


def convert_line(line: str) -> str:
    """``line`` with US-only measurements replaced by metric ones.

    Where the line already gives a metric equivalent next to the US amount,
    the US amount is dropped and the author's number kept. Otherwise it is
    converted and rounded.
    """
    line = _drop_us_duplicates(line)
    return PATTERN.sub(lambda m: _convert(m, line).proposed, line)


def needs_check(before: str, after: str) -> bool:
    """True if a converted amount may now sit next to a metric one the author gave.

    E.g. "1 lb beef, about 500g" becomes "450 g beef, about 500g".
    """
    before = _drop_us_duplicates(before)
    return bool(PATTERN.search(before)) and bool(_HAS_METRIC.search(before))


SECTIONS = ("Ingredients", "Directions", "Notes")


def convert_body(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Convert the Ingredients, Directions and Notes sections of a recipe body.

    Returns the new body and the ``(before, after)`` lines that changed.
    """
    out, changes, active = [], [], False
    for line in body.splitlines(keepends=True):
        if line.startswith("## "):
            active = line[3:].strip() in SECTIONS
        elif active and not line.lstrip().startswith("!["):
            new = convert_line(line)
            if new != line:
                changes.append((line.strip(), new.strip()))
                line = new
        out.append(line)
    return "".join(out), changes
