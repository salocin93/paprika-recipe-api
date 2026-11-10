#!/usr/bin/env python3
"""Read-only audit of the recipe collection → reports/recipe_audit.md.

Reports:
  * likely duplicates (same title, same source URL, or near-identical ingredients)
  * junk / placeholder recipes
  * recipes with gaps (no ingredients, directions, source, photo, category)
  * US-only units (pounds, ounces, gallons/quarts/pints, inches, °F) with a
    proposed rounded metric replacement. Cups and spoons are left alone.

Never writes to recipes.

Usage:
    uv run audit_recipes.py
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from paprika_harness import CategoryMap, RecipeFileManager, load_settings
from paprika_harness.diet import extract_ingredients, section
from paprika_harness.units import LABELS, find_us_units

JUNK = re.compile(r"^(test\b|untitled|new recipe|https?://)|recipe name|title$", re.I)


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", t.casefold()).strip()


def ing_set(recipe) -> frozenset[str]:
    return frozenset(norm_title(i) for i in extract_ingredients(recipe.body) if i.strip())


def find_duplicates(recipes) -> list[tuple[str, list]]:
    groups: list[tuple[str, list]] = []
    seen: set[frozenset] = set()

    def add(reason, members):
        key = frozenset(r.path.name for r in members)
        if len(key) > 1 and key not in seen:
            seen.add(key)
            groups.append((reason, sorted(members, key=lambda r: r.path.name)))

    by_title, by_url = defaultdict(list), defaultdict(list)
    for r in recipes:
        by_title[norm_title(r.title)].append(r)
        url = (r.frontmatter.get("source_url") or "").strip().rstrip("/")
        if url:
            by_url[url].append(r)
    for g in by_title.values():
        add("same title", g)
    for g in by_url.values():
        add("same source URL", g)
    sets = [(r, s) for r in recipes if len(s := ing_set(r)) >= 4]
    for (a, sa), (b, sb) in combinations(sets, 2):
        j = len(sa & sb) / len(sa | sb)
        if j >= 0.8:
            add(f"{j:.0%} of ingredient lines identical", [a, b])
    return groups


def find_gaps(recipes, cmap) -> dict[str, list]:
    gaps: dict[str, list] = defaultdict(list)
    for r in recipes:
        if not extract_ingredients(r.body):
            gaps["No ingredient list"].append(r)
        if not section(r.body, "Directions"):
            gaps["No directions"].append(r)
        if not (r.frontmatter.get("source") or r.frontmatter.get("source_url")):
            gaps["No source"].append(r)
        if not re.search(r"^!\[", r.body, re.M):
            gaps["No photo"].append(r)
        paths = [cmap.display(u) for u in r.categories]
        if not any(p.startswith("By Type") for p in paths):
            gaps["No By Type category"].append(r)
        if not any(p.startswith("By Region") for p in paths):
            gaps["No By Region category"].append(r)
        if any(not cmap.is_resolved(u) for u in r.categories):
            gaps["Points to a deleted category"].append(r)
    return gaps


def scan_units(recipes):
    """Per recipe: list of (where, line, hits)."""
    out = {}
    for r in recipes:
        rows = []
        for where, lines in (("ingredient", extract_ingredients(r.body)),
                             ("directions", section(r.body, "Directions").splitlines())):
            for line in lines:
                hits = find_us_units(line)
                if hits:
                    rows.append((where, line.strip(), hits))
        if rows:
            out[r.path.name] = (r, rows)
    return out


def render(recipes, cmap) -> str:
    out = ["# Recipe audit\n", f"{len(recipes)} recipes checked. Nothing was changed.\n"]

    dups = find_duplicates(recipes)
    out.append(f"## Likely duplicates ({len(dups)} groups)\n")
    out.append("| Why | Recipe | File | Created | Rating | Ingredients |\n|---|---|---|---|---:|---:|")
    for reason, members in dups:
        for i, r in enumerate(members):
            out.append(f"| {reason if i == 0 else ''} | {r.title[:60]} | `{r.path.name[:50]}` | "
                       f"{str(r.frontmatter.get('created', ''))[:10]} | {r.frontmatter.get('rating', 0)} | "
                       f"{len(extract_ingredients(r.body))} |")
    out.append("")

    junk = [r for r in recipes if JUNK.search(r.title.strip())]
    out.append(f"## Junk or placeholder titles ({len(junk)})\n")
    out += [f"- {r.title[:80]} (`{r.path.name[:60]}`)" for r in junk] + [""]

    gaps = find_gaps(recipes, cmap)
    out.append("## Gaps\n")
    out.append("| Gap | Recipes |\n|---|---:|")
    order = ["No ingredient list", "No directions", "No source", "No photo",
             "No By Type category", "No By Region category", "Points to a deleted category"]
    out += [f"| {g} | {len(gaps.get(g, []))} |" for g in order] + [""]
    for g in order:
        if g in ("No By Type category", "No By Region category") or not gaps.get(g):
            continue
        out.append(f"### {g} ({len(gaps[g])})\n")
        out += [f"- {r.title[:80]}" for r in sorted(gaps[g], key=lambda r: r.title.casefold())] + [""]

    units = scan_units(recipes)
    per_unit = Counter()
    per_unit_recipes = defaultdict(set)
    for name, (_, rows) in units.items():
        for _, _, hits in rows:
            for h in hits:
                per_unit[h.unit] += 1
                per_unit_recipes[h.unit].add(name)
    out.append(f"## US-only units ({len(units)} recipes)\n")
    out.append("Cups, tablespoons and teaspoons are not counted.\n")
    out.append("| Unit | Recipes | Occurrences |\n|---|---:|---:|")
    out += [f"| {LABELS[u]} | {len(per_unit_recipes[u])} | {n} |" for u, n in per_unit.most_common()] + [""]
    out.append("### Proposed replacements\n")
    out.append("*(check)* marks ounces of a liquid, where the recipe may mean volume or weight.\n")
    for name in sorted(units, key=lambda n: units[n][0].title.casefold()):
        r, rows = units[name]
        out.append(f"**{r.title[:80]}**\n")
        for where, line, hits in rows:
            changes = "; ".join(f"`{h.original}` → `{h.proposed}`" + (" *(check)*" if h.uncertain else "") for h in hits)
            out.append(f"- {where}: {line[:110]}{'…' if len(line) > 110 else ''}  \n  {changes}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--category-map", type=Path, default=settings.category_map_file)
    ap.add_argument("--report", type=Path, default=Path("reports/recipe_audit.md"))
    args = ap.parse_args(argv)

    recipes = RecipeFileManager(args.recipes_dir).scan().recipes
    cmap = CategoryMap.load(args.category_map)
    text = render(recipes, cmap)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(text, encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("## ") or (line.startswith("| ") and "Recipe |" not in line and "`" not in line):
            print(line)
    print(f"\nFull report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
