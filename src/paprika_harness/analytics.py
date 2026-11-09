"""Category structure analysis."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations

from .categories import CategoryMap
from .file_manager import ScanResult
from .recipe import ParseError, Recipe

# Groups of names that usually mean overlapping things. Matching is on the
# normalised form (see ``normalize``). Extend freely.
SEMANTIC_GROUPS: list[set[str]] = [
    {"baking", "bake", "dessert", "sweet", "cake", "cookie", "pastry", "treat", "nachspeise", "kuchen", "gebäck"},
    {"main", "main course", "main dish", "entree", "dinner", "hauptgericht", "hauptspeise"},
    {"starter", "appetizer", "appetiser", "snack", "finger food", "tapa", "vorspeise"},
    {"side", "side dish", "beilage"},
    {"vegetarian", "veggie", "vegan", "meatless", "plant based", "vegetarisch"},
    {"soup", "stew", "suppe", "eintopf"},
    {"salad", "salat"},
    {"breakfast", "brunch", "frühstück"},
    {"drink", "beverage", "cocktail", "getränk"},
    {"sauce", "dip", "dressing", "condiment", "soße", "sosse"},
    {"bread", "brot"},
    {"chicken", "poultry", "hähnchen", "huhn", "geflügel"},
    {"meat", "beef", "pork", "lamb", "fleisch"},
    {"fish", "seafood", "fisch", "fischgericht"},
    {"asparagus", "spargel"},
    {"german", "deutsch", "german and austrian"},
    {"mediterranean", "mediteranian"},
    {"stir fry", "wok"},
    {"air fryer", "air frier"},
]


def normalize(name: str) -> str:
    s = name.casefold().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    words = []
    for w in s.split():
        # Crude stemming so singular/plural forms collide:
        # cookie/cookies -> cooki, berry/berries -> berri, dish/dishes -> dish
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "i"
        elif len(w) > 3 and w.endswith("ie"):
            w = w[:-2] + "i"
        elif len(w) > 3 and w.endswith("y"):
            w = w[:-1] + "i"
        elif len(w) > 4 and w.endswith("hes"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(words)


def _norm_groups() -> list[set[str]]:
    return [{normalize(w) for w in g} for g in SEMANTIC_GROUPS]


@dataclass
class SimilarPair:
    a: str  # uid
    b: str  # uid
    reason: str
    score: float


@dataclass
class CategoryReport:
    generated_at: datetime
    total_files: int
    recipes: list[Recipe]
    errors: list[ParseError]
    category_map: CategoryMap
    recipes_by_category: dict[str, list[Recipe]] = field(default_factory=dict)
    uncategorized: list[Recipe] = field(default_factory=list)
    similar: list[SimilarPair] = field(default_factory=list)

    @property
    def unresolved_uids(self) -> list[str]:
        return [u for u in self.recipes_by_category if not self.category_map.is_resolved(u)]


def analyze_categories(
    scan: ScanResult,
    category_map: CategoryMap,
    *,
    fuzzy_threshold: float = 0.8,
    overlap_threshold: float = 0.75,
    overlap_min_size: int = 3,
) -> CategoryReport:
    by_cat: dict[str, list[Recipe]] = defaultdict(list)
    uncategorized: list[Recipe] = []
    for recipe in scan.recipes:
        cats = recipe.categories
        if not cats:
            uncategorized.append(recipe)
        for uid in dict.fromkeys(cats):  # de-dupe within a recipe, keep order
            by_cat[uid].append(recipe)

    # Include categories defined in the map that no recipe uses (count 0).
    for uid in category_map.names:
        by_cat.setdefault(uid, [])

    report = CategoryReport(
        generated_at=datetime.now(),
        total_files=scan.total_files,
        recipes=scan.recipes,
        errors=scan.errors,
        category_map=category_map,
        recipes_by_category=dict(sorted(by_cat.items(), key=lambda kv: (-len(kv[1]), category_map.name(kv[0]).casefold()))),
        uncategorized=sorted(uncategorized, key=lambda r: r.title.casefold()),
    )
    report.similar = find_similar(
        report.recipes_by_category, category_map,
        fuzzy_threshold=fuzzy_threshold,
        overlap_threshold=overlap_threshold,
        overlap_min_size=overlap_min_size,
    )
    return report


def find_similar(
    by_cat: dict[str, list[Recipe]],
    category_map: CategoryMap,
    *,
    fuzzy_threshold: float,
    overlap_threshold: float,
    overlap_min_size: int,
) -> list[SimilarPair]:
    pairs: list[SimilarPair] = []
    members = {uid: {id(r) for r in rs} for uid, rs in by_cat.items()}

    groups = _norm_groups()
    for a, b in combinations(by_cat, 2):
        # Parent/child pairs overlap by design — not duplicates.
        if a in category_map.ancestors(b) or b in category_map.ancestors(a):
            continue
        reasons: list[tuple[str, float]] = []

        if category_map.is_resolved(a) and category_map.is_resolved(b):
            na, nb = category_map.name(a), category_map.name(b)
            ka, kb = normalize(na), normalize(nb)
            if na == nb:
                reasons.append(("same name in different branches", 1.0))
            elif na.strip().casefold() == nb.strip().casefold():
                reasons.append(("case/whitespace mismatch", 1.0))
            elif ka == kb:
                reasons.append(("same after normalising (plural/punctuation)", 1.0))
            else:
                ratio = SequenceMatcher(None, ka, kb).ratio()
                if ratio >= fuzzy_threshold:
                    reasons.append((f"similar spelling ({ratio:.0%})", ratio))
                ta, tb = set(ka.split()), set(kb.split())
                if ta and tb and (ta < tb or tb < ta):
                    reasons.append(("one name contains the other", 0.8))
                if any(ka in g and kb in g for g in groups):
                    reasons.append(("related meaning", 0.7))

        sa, sb = members[a], members[b]
        if min(len(sa), len(sb)) >= overlap_min_size:
            overlap = len(sa & sb) / min(len(sa), len(sb))
            if overlap >= overlap_threshold:
                smaller, larger = (a, b) if len(sa) <= len(sb) else (b, a)
                reasons.append((
                    f"{overlap:.0%} of '{category_map.name(smaller)}' recipes are also in "
                    f"'{category_map.name(larger)}'", overlap,
                ))

        if reasons:
            pairs.append(SimilarPair(a, b, "; ".join(r for r, _ in reasons), max(s for _, s in reasons)))

    return sorted(pairs, key=lambda p: -p.score)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _md_escape(s: str) -> str:
    return s.replace("|", "\\|")


def _examples(recipes: list[Recipe], k: int = 3) -> str:
    titles = sorted(r.title for r in recipes)[:k]
    return "; ".join(titles) + (" …" if len(recipes) > k else "")


def _label(cm: CategoryMap, uid: str) -> str:
    """Readable label; unresolved UIDs are shortened for console output."""
    return cm.display(uid) if cm.is_resolved(uid) else f"[{uid[:8]}]"


def render_markdown(report: CategoryReport) -> str:
    cm = report.category_map
    n = len(report.recipes)
    used = {u: rs for u, rs in report.recipes_by_category.items() if rs}
    unused = [u for u, rs in report.recipes_by_category.items() if not rs]
    out: list[str] = []
    w = out.append

    w("# Recipe Category Analysis\n")
    w(f"_Generated {report.generated_at:%Y-%m-%d %H:%M}_\n")

    w("## Summary\n")
    w(f"| Metric | Value |\n|---|---|")
    w(f"| Recipe files scanned | {report.total_files} |")
    w(f"| Parsed successfully | {n} |")
    w(f"| Failed to parse | {len(report.errors)} |")
    w(f"| Distinct categories in use | {len(used)} |")
    w(f"| Categories defined but unused | {len(unused)} |")
    w(f"| Recipes with no category | {len(report.uncategorized)} ({len(report.uncategorized) / n:.0%}) |" if n else "| Recipes with no category | 0 |")
    multi = sum(1 for r in report.recipes if len(r.categories) > 1)
    w(f"| Recipes in 2+ categories | {multi} |")
    w("")

    if report.unresolved_uids:
        w("> **Note:** " + (
            f"{len(report.unresolved_uids)} category UIDs could not be resolved to names. "
            "Paprika's Markdown export stores only UIDs; run `uv run fetch_categories.py` "
            "(or write `category_map.yaml` by hand) to see names. Name-based duplicate "
            "detection only runs on resolved categories.\n"
        ))

    w("## Recipes per category\n")
    w("| # | Category | Recipes | % of all | Examples |\n|---:|---|---:|---:|---|")
    for i, (uid, rs) in enumerate(used.items(), 1):
        w(f"| {i} | {_md_escape(cm.display(uid))} | {len(rs)} | {len(rs) / n:.1%} "
          f"| {_md_escape(_examples(rs))} |")
    w("")
    if unused:
        w("### Defined but unused\n")
        for uid in unused:
            w(f"- {cm.display(uid)}")
        w("")

    w("## Potential duplicate / overlapping categories\n")
    if report.similar:
        w("| Category A | Category B | Why |\n|---|---|---|")
        for p in report.similar:
            w(f"| {_md_escape(cm.display(p.a))} ({len(report.recipes_by_category[p.a])}) "
              f"| {_md_escape(cm.display(p.b))} ({len(report.recipes_by_category[p.b])}) "
              f"| {_md_escape(p.reason)} |")
    else:
        w("_None detected._")
    w("")

    w(f"## Recipes with no category ({len(report.uncategorized)})\n")
    for r in report.uncategorized:
        w(f"- {r.title} — `{r.path.name}`")
    w("")

    if report.errors:
        w(f"## Files that failed to parse ({len(report.errors)})\n")
        for e in report.errors:
            w(f"- `{e.path.name}`: {e.reason}")
        w("")

    w("## Recipes by category\n")
    for uid, rs in used.items():
        w(f"<details><summary>{cm.display(uid)} ({len(rs)})</summary>\n")
        for r in sorted(rs, key=lambda r: r.title.casefold()):
            w(f"- {r.title}")
        w("\n</details>\n")

    return "\n".join(out)


def render_console(report: CategoryReport, top: int = 25) -> str:
    cm = report.category_map
    used = [(u, rs) for u, rs in report.recipes_by_category.items() if rs]
    lines = [
        f"Recipes scanned: {report.total_files}  (parsed {len(report.recipes)}, failed {len(report.errors)})",
        f"Categories in use: {len(used)}  |  Uncategorized recipes: {len(report.uncategorized)}",
        f"Category names resolved: {len(used) - len(report.unresolved_uids)}/{len(used)}",
        "",
        f"Top {min(top, len(used))} categories:",
    ]
    width = min(max((len(_label(cm, u)) for u, _ in used[:top]), default=10), 50)
    for uid, rs in used[:top]:
        hint = "" if cm.is_resolved(uid) else f"  e.g. {_examples(rs, 2)}"
        lines.append(f"  {_label(cm, uid):<{width}}  {len(rs):>4}{hint}")
    lines += ["", f"Potential duplicates/overlaps: {len(report.similar)}"]
    for p in report.similar[:10]:
        a, b = _label(cm, p.a), _label(cm, p.b)
        reason = p.reason.replace(cm.name(p.a), a).replace(cm.name(p.b), b)
        lines.append(f"  • {a}  ↔  {b}: {reason}")
    return "\n".join(lines)


def to_json(report: CategoryReport) -> dict:
    cm = report.category_map
    return {
        "generated_at": report.generated_at.isoformat(timespec="seconds"),
        "total_files": report.total_files,
        "parsed": len(report.recipes),
        "errors": [{"file": e.path.name, "reason": e.reason} for e in report.errors],
        "categories": [
            {"uid": u, "name": cm.name(u), "resolved": cm.is_resolved(u), "count": len(rs),
             "recipes": sorted(r.title for r in rs)}
            for u, rs in report.recipes_by_category.items()
        ],
        "uncategorized": [{"title": r.title, "file": r.path.name} for r in report.uncategorized],
        "similar": [
            {"a": cm.name(p.a), "b": cm.name(p.b), "a_uid": p.a, "b_uid": p.b,
             "reason": p.reason, "score": round(p.score, 3)}
            for p in report.similar
        ],
    }
