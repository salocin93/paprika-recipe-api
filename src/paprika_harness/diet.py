"""Diet classification: LLM prompt, keyword cross-check and result cache."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from .recipe import Recipe

Diet = Literal["vegan", "vegetarian", "pescatarian", "meat"]
DIETS: tuple[Diet, ...] = ("vegan", "vegetarian", "pescatarian", "meat")
# Higher = less restrictive. A recipe's diet is the lowest rank it satisfies.
RANK: dict[str, int] = {d: i for i, d in enumerate(DIETS)}

PROMPT_VERSION = "1"  # bump when the prompt changes to invalidate the cache

SYSTEM_PROMPT = """\
You classify recipes by diet. Choose exactly one label, the most restrictive one the recipe satisfies AS WRITTEN:

- "vegan": no animal products at all: no meat, poultry, fish, seafood, dairy (milk, butter, cream, cheese, yogurt, ghee), eggs, honey or gelatin.
- "vegetarian": no meat, poultry, fish or seafood, and nothing made from them (meat/chicken/fish stock, gelatin, lard, fish sauce, anchovies, shrimp paste, oyster sauce, bonito/dashi). Dairy, eggs and honey are allowed. Treat cheeses such as Parmesan as vegetarian.
- "pescatarian": contains fish or seafood, or products made from them (fish sauce, anchovies, Worcestershire sauce, shrimp paste, oyster sauce, bonito/dashi), but no meat or poultry.
- "meat": contains meat or poultry, or products made from them (bacon, ham, sausage, chicken/beef stock, lard, gelatin).

Rules:
- Read every ingredient line. Also check the directions and notes for ingredients that are not in the list.
- Ingredient lists may be in English or German (e.g. Hähnchen = chicken, Speck = bacon, Lachs = salmon, Sahne = cream).
- If an ingredient offers alternatives ("chicken or vegetable stock"), classify by the first option.
- Count optional ingredients and garnishes too.
- For ambiguous ready-made products (plain "stock", Thai curry paste, which usually contains shrimp paste, etc.), assume the typical commercial version.
- Confidence is 0 to 1. Use 0.7 or lower when the label depends on an optional ingredient, an alternative, an ambiguous product, or when no ingredient list is given.
- "animal_ingredients": the ingredient(s) that decided the label (empty list for vegan).
- "reason": one short sentence."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diet": {"type": "string", "enum": list(DIETS)},
        "confidence": {"type": "number"},
        "animal_ingredients": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["diet", "confidence", "animal_ingredients", "reason"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- #
# Recipe text extraction
# --------------------------------------------------------------------------- #
def section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else ""


def extract_ingredients(body: str) -> list[str]:
    """Return the bullet lines under the ``## Ingredients`` heading."""
    return [
        line.strip().lstrip("-*").strip()
        for line in section(body, "Ingredients").splitlines()
        if line.strip().startswith(("-", "*"))
    ]


def build_prompt(recipe: Recipe, *, max_chars: int = 2500) -> str:
    ingredients = extract_ingredients(recipe.body)
    parts = [f"Recipe: {recipe.title}", ""]
    if ingredients:
        parts += ["Ingredients:", *[f"- {i}" for i in ingredients]]
    else:
        parts.append("Ingredients: (no ingredient list given)")
    for heading in ("Directions", "Notes"):
        text = section(recipe.body, heading)
        if text:
            if len(text) > max_chars:
                text = text[:max_chars] + " …"
            parts += ["", f"{heading}:", text]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Keyword cross-check (independent of the LLM)
# --------------------------------------------------------------------------- #
_EXCLUDE = [
    r"egg ?plants?", r"butternut", r"(peanut|almond|cashew|nut|seed|apple|cocoa|shea|vegan|plant[- ]based) butter",
    r"(almond|oat|soy|soya|coconut|rice|cashew|hazelnut|plant[- ]based|vegan|dairy[- ]free) (milk|cream|yogh?urt|cheese|creamer)",
    r"cream of tartar", r"(vegetable|veggie|mushroom) (stock|broth|bouillon)", r"butter ?beans?", r"butter ?(head|lettuce)",
    r"oyster mushrooms?", r"egg[- ]?(less|free)", r"cheesecloth", r"hamburger buns?", r"nutritional yeast",
    r"kokos(nuss)?(milch|sahne|creme)", r"gemüse(brühe|fond)", r"erdnussbutter", r"eierschwammerl",
    r"(hafer|soja|mandel|reis)(milch|sahne|drink)", r"butterbohnen", r"butternuss", r"honeydew", r"butterfl\w*",
]
_MEAT = [
    r"chicken", r"beef", r"pork", r"lamb", r"mutton", r"veal", r"bacon", r"\bham\b", r"prosciutto", r"pancetta",
    r"guanciale", r"chorizo", r"salami", r"pepperoni", r"sausage", r"\bduck", r"turkey", r"goose", r"venison",
    r"rabbit", r"quail", r"gelatine?", r"\blard\b", r"\bsuet\b", r"bone marrow", r"oxtail", r"brisket", r"meatballs?",
    r"\bliver\b", r"\bspeck\b", r"\bmince(d)? meat", r"hähnchen", r"\bhuhn", r"hühner", r"geflügel", r"rindfleisch", r"\brinder", r"\brinds",
    r"schwein", r"\bente\b", r"\benten", r"hackfleisch", r"\bhack\b", r"wurst", r"schinken", r"\blamm", r"kalb",
]
_FISH = [
    r"\bfish", r"salmon", r"tuna", r"\bcod\b", r"anchov", r"sardine", r"mackerel", r"trout", r"tilapia", r"halibut",
    r"haddock", r"snapper", r"sea ?bass", r"shrimp", r"prawn", r"\bcrab", r"lobster", r"mussel", r"\bclams?\b",
    r"oyster", r"scallop", r"squid", r"calamari", r"octopus", r"bonito", r"katsuobushi", r"\bdashi", r"worcestershire",
    r"belacan", r"terasi", r"ikan bilis", r"nam pla", r"caviar", r"\broe\b", r"lachs", r"fisch", r"garnele",
    r"sardelle", r"thunfisch", r"krabbe", r"muschel", r"tintenfisch", r"forelle", r"kabeljau",
]
_ANIMAL = [
    r"\beggs?\b", r"yolks?", r"egg whites?", r"butter", r"\bghee\b", r"\bmilk", r"cream", r"crème fraîche", r"cheese",
    r"parmesan", r"parmigiano", r"pecorino", r"mozzarella", r"ricotta", r"\bfeta\b", r"mascarpone", r"burrata",
    r"halloumi", r"gruy[eè]re", r"cheddar", r"yogh?urt", r"buttermilk", r"honey", r"paneer", r"labneh", r"kefir",
    r"mayo(nnaise)?", r"\bei\b", r"\beier", r"eigelb", r"eiweiß", r"milch", r"sahne", r"käse", r"joghurt",
    r"quark", r"honig", r"schmand", r"crème", r"rahm",
]


def _compile(pats: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I)


_EXCLUDE_RE, _MEAT_RE, _FISH_RE, _ANIMAL_RE = map(_compile, (_EXCLUDE, _MEAT, _FISH, _ANIMAL))


@dataclass
class KeywordScan:
    floor: Diet  # least restrictive label implied by keywords found
    hits: dict[str, list[str]] = field(default_factory=dict)


def keyword_scan(ingredients: list[str]) -> KeywordScan:
    """Find obvious animal ingredients. Used only to sanity-check the LLM."""
    hits: dict[str, list[str]] = {"meat": [], "pescatarian": [], "vegetarian": []}
    for line in ingredients:
        text = _EXCLUDE_RE.sub(" ", line.lower())
        for label, rx in (("meat", _MEAT_RE), ("pescatarian", _FISH_RE), ("vegetarian", _ANIMAL_RE)):
            if rx.search(text):
                hits[label].append(line)
                break  # one class per line (most severe wins)
    floor: Diet = next((d for d in ("meat", "pescatarian", "vegetarian") if hits[d]), "vegan")  # type: ignore[assignment]
    return KeywordScan(floor, {k: v for k, v in hits.items() if v})


# --------------------------------------------------------------------------- #
# Results + cache
# --------------------------------------------------------------------------- #
@dataclass
class Classification:
    uid: str
    title: str
    file: str
    diet: Diet
    confidence: float
    animal_ingredients: list[str]
    reason: str
    keyword_floor: Diet
    keyword_hits: dict[str, list[str]]
    has_ingredients: bool
    model: str
    fingerprint: str
    classified_at: str

    @property
    def conflict(self) -> bool:
        """LLM label is *more permissive* than what keywords clearly show."""
        return RANK[self.diet] < RANK[self.keyword_floor]

    def needs_review(self, min_confidence: float) -> list[str]:
        why = []
        if self.conflict:
            hits = "; ".join(self.keyword_hits.get(self.keyword_floor, []))[:150]
            why.append(f"keywords suggest {self.keyword_floor} ({hits})")
        if self.confidence < min_confidence:
            why.append(f"low confidence {self.confidence:.2f}")
        if not self.has_ingredients:
            why.append("no ingredient list")
        return why


class LLMLike(Protocol):
    model: str

    def complete_json(self, system: str, user: str, schema: dict, *, name: str = ...) -> dict: ...


def fingerprint(recipe: Recipe, model: str) -> str:
    h = hashlib.sha256()
    for part in (PROMPT_VERSION, model, build_prompt(recipe)):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def classify_recipe(recipe: Recipe, llm: LLMLike) -> Classification:
    raw = llm.complete_json(SYSTEM_PROMPT, build_prompt(recipe), RESPONSE_SCHEMA, name="diet_classification")
    diet = str(raw.get("diet", "")).strip().lower()
    if diet not in RANK:
        raise ValueError(f"model returned invalid diet {raw.get('diet')!r}")
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    ingredients = extract_ingredients(recipe.body)
    scan = keyword_scan(ingredients)
    return Classification(
        uid=recipe.uid or recipe.path.stem,
        title=recipe.title,
        file=recipe.path.name,
        diet=diet,  # type: ignore[arg-type]
        confidence=confidence,
        animal_ingredients=[str(x) for x in raw.get("animal_ingredients") or []],
        reason=str(raw.get("reason", "")).strip(),
        keyword_floor=scan.floor,
        keyword_hits=scan.hits,
        has_ingredients=bool(ingredients),
        model=llm.model,
        fingerprint=fingerprint(recipe, llm.model),
        classified_at=datetime.now().isoformat(timespec="seconds"),
    )


class ClassificationCache:
    """JSON file of results keyed by recipe uid, written after every update."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt cache {path}: {exc}. Delete it to start over.") from exc

    def get(self, recipe: Recipe, model: str) -> Classification | None:
        entry = self._data.get(recipe.uid or recipe.path.stem)
        if not entry or entry.get("fingerprint") != fingerprint(recipe, model):
            return None
        try:
            return Classification(**entry)
        except TypeError:
            return None  # schema changed; reclassify

    def put(self, c: Classification) -> None:
        with self._lock:
            self._data[c.uid] = asdict(c)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
