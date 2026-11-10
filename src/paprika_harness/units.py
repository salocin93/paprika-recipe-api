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
    "F": r"°\s?F\b|degrees\s+F(?:ahrenheit)?\b|F(?:ahrenheit)\b",
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
    uncertain: bool = False  # e.g. "8 oz milk": weight or volume?


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
    uncertain = unit == "oz" and bool(_LIQUID.search(line))
    conv = _CONVERT["floz" if uncertain else unit]
    values = [conv(_num(m.group(g))) for g in ("a", "b") if m.group(g)]
    # Use one unit for both ends of a range: "2-3 lb" -> "0.9–1.4 kg".
    if len(values) == 2 and values[0][1] != values[1][1]:
        values = [(round(v / 1000, 1), values[1][1]) if u != values[1][1] else (v, u) for v, u in values]
    sep = m.group("sep") or ""
    sep = {"x": " x ", "×": " x ", "by": " x ", "to": " to "}.get(sep.lower(), "–" if sep else "")
    out_unit = values[-1][1]
    space = "" if out_unit == "°C" else " "
    return Hit(unit, m.group(0), sep.join(_fmt(v) for v, _ in values) + space + out_unit, uncertain)


def find_us_units(line: str) -> list[Hit]:
    return [_convert(m, line) for m in PATTERN.finditer(line)]


def convert_line(line: str) -> str:
    """``line`` with every US-only measurement replaced by its metric proposal."""
    return PATTERN.sub(lambda m: _convert(m, line).proposed, line)
