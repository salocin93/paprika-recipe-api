"""LLM suggestions for missing "By Type" / "By Region" categories.

The LLM may only choose from the categories that already exist under each
branch (or "none"). Results are cached per recipe, keyed by a fingerprint of
the prompt, model and the list of options, so adding a category in Paprika
re-asks the LLM.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .categories import CategoryMap
from .diet import LLMLike, build_prompt
from .recipe import Recipe

PROMPT_VERSION = "1"
NONE = "none"

SYSTEM_PROMPT = """You file recipes into an existing category tree.

For each branch you get a fixed list of options. Pick the single best option
per branch, or "none" if no option really fits. Do not invent categories.

- type: what kind of dish it is. Prefer the most specific fitting option
  (e.g. a chicken curry is "Chicken" or "Stews", not "Other Mains"). Use
  "Other Mains" only for a main course that fits nothing else.
- region: the cuisine the dish belongs to. Prefer the most specific option
  (a country over a continent). Judge by the dish itself, not by the language
  the recipe is written in or the website it came from. Use "none" for dishes
  without a clear regional identity (e.g. granola, a generic vinaigrette).

confidence is 0..1 for each choice. reason: one short sentence."""


@dataclass(frozen=True)
class Branch:
    key: str                  # "type" / "region"
    root_uid: str
    options: dict[str, str]   # label shown to the LLM -> category uid
    members: frozenset[str]   # every uid in the branch except the root


def load_branch(cmap: CategoryMap, key: str, root: str) -> Branch:
    """Options are every category below ``root``, labelled by path below the root."""
    root_uid = cmap.resolve(root)
    options = {}
    for uid in cmap.names:
        chain = cmap.ancestors(uid)
        if root_uid in chain:
            below = [cmap.name(a) for a in reversed(chain[: chain.index(root_uid)])] + [cmap.name(uid)]
            options[" › ".join(below)] = uid
    if not options:
        raise KeyError(f"category {root!r} has no sub-categories")
    return Branch(key, root_uid, dict(sorted(options.items())), frozenset(options.values()))


def schema(branches: list[Branch]) -> dict:
    props: dict = {}
    for b in branches:
        props[b.key] = {"type": "string", "enum": [*b.options, NONE]}
        props[f"{b.key}_confidence"] = {"type": "number"}
    props["reason"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def user_prompt(recipe: Recipe, branches: list[Branch]) -> str:
    opts = "\n".join(f"{b.key} options: {' | '.join(b.options)} | {NONE}" for b in branches)
    return f"{opts}\n\n{build_prompt(recipe, max_chars=1200)}"


@dataclass
class Suggestion:
    uid: str
    title: str
    file: str
    choices: dict[str, str]          # branch key -> option label or "none"
    confidence: dict[str, float]
    reason: str
    model: str
    fingerprint: str
    suggested_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


def fingerprint(recipe: Recipe, model: str, branches: list[Branch]) -> str:
    h = hashlib.sha256()
    for part in (PROMPT_VERSION, model, user_prompt(recipe, branches)):
        h.update(part.encode("utf-8") + b"\0")
    return h.hexdigest()[:16]


def suggest(recipe: Recipe, llm: LLMLike, branches: list[Branch]) -> Suggestion:
    raw = llm.complete_json(SYSTEM_PROMPT, user_prompt(recipe, branches), schema(branches), name="category_suggestion")
    choices, conf = {}, {}
    for b in branches:
        choice = str(raw.get(b.key, NONE)).strip()
        if choice != NONE and choice not in b.options:
            # Tolerate a bare leaf name ("Thai" for "Asian › Thai") if it is unambiguous.
            matches = [o for o in b.options if o.split(" › ")[-1].casefold() == choice.casefold()]
            if len(matches) != 1:
                raise ValueError(f"model returned unknown {b.key} option {choice!r}")
            choice = matches[0]
        choices[b.key] = choice
        try:
            conf[b.key] = max(0.0, min(1.0, float(raw.get(f"{b.key}_confidence", 0))))
        except (TypeError, ValueError):
            conf[b.key] = 0.0
    return Suggestion(
        uid=recipe.uid or recipe.path.stem, title=recipe.title, file=recipe.path.name,
        choices=choices, confidence=conf, reason=str(raw.get("reason", "")).strip(),
        model=llm.model, fingerprint=fingerprint(recipe, llm.model, branches),
    )


class SuggestionCache:
    """JSON file of suggestions keyed by recipe uid, written after every update."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt cache {path}: {exc}. Delete it to start over.") from exc

    def get(self, recipe: Recipe, model: str, branches: list[Branch]) -> Suggestion | None:
        entry = self._data.get(recipe.uid or recipe.path.stem)
        if not entry or entry.get("fingerprint") != fingerprint(recipe, model, branches):
            return None
        try:
            return Suggestion(**entry)
        except TypeError:
            return None

    def put(self, s: Suggestion) -> None:
        with self._lock:
            self._data[s.uid] = asdict(s)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
