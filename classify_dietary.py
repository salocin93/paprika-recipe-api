#!/usr/bin/env python3
"""Classify recipes as Vegan / Vegetarian / Pescatarian / Meat with an LLM
and tag them with the matching "By Diet" category.

Dry run by default. Results are cached in reports/diet_classifications.json,
so the LLM is only called for new or changed recipes. Review
reports/diet_review.md, then re-run with --apply (no new LLM calls).

Recipes are only tagged when the LLM is confident and the keyword check
agrees. Anything else is listed under "Needs review".

Usage:
    uv run classify_dietary.py [--limit N] [--workers 4]   # classify + report
    uv run classify_dietary.py --apply                     # write categories
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from paprika_harness import CategoryMap, RecipeFileManager, load_settings
from paprika_harness.config import ConfigError
from paprika_harness.diet import DIETS, ClassificationCache
from paprika_harness.llm import LLMClient
from paprika_harness.processors import DietaryClassifier, Pipeline, Status
from paprika_harness.processors.dietary import DIET_NAMES, resolve_diet_categories


def render_review(classifier: DietaryClassifier, results, *, tagging: bool) -> str:
    cs = classifier.classifications
    counts = Counter(c.diet for c in cs.values())
    flagged = [(r, cs[r.recipe.path.name]) for r in results if r.status is Status.SKIPPED]
    out = ["# Diet classification review\n"]
    out.append(f"Model: `{classifier.llm.model}`, min confidence {classifier.min_confidence}\n")
    out.append("| Diet | Recipes |\n|---|---:|")
    out += [f"| {DIET_NAMES[d]} | {counts.get(d, 0)} |" for d in DIETS]
    out.append(f"| **Needs review (not tagged)** | {len(flagged)} |\n")
    if not tagging:
        out.append("> \"By Diet\" categories not found, so this is classify-only. Nothing will be tagged.\n")

    out.append(f"## Needs review ({len(flagged)})\n")
    out.append("| Recipe | LLM says | Conf. | Why flagged | LLM reason |\n|---|---|---:|---|---|")
    for r, c in sorted(flagged, key=lambda x: x[1].title.casefold()):
        why = r.message.removeprefix("review: ")
        out.append(f"| {c.title} | {c.diet} | {c.confidence:.2f} | {why} | {c.reason} |".replace("\n", " "))
    out.append("")

    swaps = sorted((c for c in cs.values() if c.uses_veg_stock_swap), key=lambda c: c.title.casefold())
    out.append(f"## Use vegetable stock ({len(swaps)})\n")
    out.append("Classified as vegan/vegetarian on the assumption that this stock is swapped for vegetable stock.\n")
    out.append("| Recipe | Diet | Stock in recipe |\n|---|---|---|")
    for c in swaps:
        out.append(f"| {c.title} | {c.diet} | {'; '.join(c.animal_stock)[:120]} |")
    out.append("")

    for d in DIETS:
        rows = sorted((c for c in cs.values() if c.diet == d), key=lambda c: c.title.casefold())
        out.append(f"## {DIET_NAMES[d]} ({len(rows)})\n")
        out.append("| Recipe | Conf. | Deciding ingredients | Reason |\n|---|---:|---|---|")
        for c in rows:
            ing = ", ".join(c.animal_ingredients)[:120]
            title = c.title + (" *(veg stock)*" if c.uses_veg_stock_swap else "")
            out.append(f"| {title} | {c.confidence:.2f} | {ing} | {c.reason} |".replace("|  |", "| |"))
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--category-map", type=Path, default=settings.category_map_file)
    ap.add_argument("--parent", default="By Diet", help='parent category of the diet categories (default "By Diet")')
    ap.add_argument("--limit", type=int, help="only the first N recipes")
    ap.add_argument("--only", nargs="+", metavar="TEXT", help="only recipes whose title contains any of these")
    ap.add_argument("--workers", type=int, default=4, help="parallel LLM requests")
    ap.add_argument("--rpm", type=float, help="max LLM requests per minute (default: LOCAL_LLM_MAX_RPM)")
    ap.add_argument("--min-confidence", type=float, default=0.75)
    ap.add_argument("--include-flagged", action="store_true", help="also tag recipes flagged for review")
    ap.add_argument("--refresh", action="store_true", help="ignore cached results and re-ask the LLM")
    ap.add_argument("--cache", type=Path, default=Path("reports/diet_classifications.json"))
    ap.add_argument("--report", type=Path, default=Path("reports/diet_review.md"))
    ap.add_argument("--apply", action="store_true", help="write categories (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        llm = LLMClient(settings.llm, max_rpm=args.rpm)
        cache = ClassificationCache(args.cache)
        cmap = CategoryMap.load(args.category_map)
        manager = RecipeFileManager(args.recipes_dir, dry_run=not args.apply, backup_dir=args.backup_dir)
    except (ConfigError, FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        diet_uids = resolve_diet_categories(cmap, args.parent)
    except KeyError as exc:
        if args.apply:
            print(f"error: {exc.args[0]}.\nCreate them in the Paprika app, then run fetch_categories.py.", file=sys.stderr)
            return 2
        print(f"note: {exc.args[0]}; classifying only.\n")
        diet_uids = None

    recipes = manager.scan().recipes
    if args.only:
        needles = [n.casefold() for n in args.only]
        recipes = [r for r in recipes if any(n in r.title.casefold() for n in needles)]
    recipes = recipes[: args.limit]

    classifier = DietaryClassifier(
        llm, cache, diet_uids=diet_uids,
        min_confidence=args.min_confidence, include_flagged=args.include_flagged,
    )

    def progress(i, n, recipe, exc):
        status = f"ERROR {exc}" if exc else classifier.cache.get(recipe, llm.model).diet
        print(f"  [{i}/{n}] {recipe.title[:60]}: {status}", flush=True)

    rpm = f", max {60 / llm.limiter.interval:g}/min" if llm.limiter.interval else ""
    print(f"Classifying with {llm.model} ({len(recipes)} recipes; cached ones are skipped{rpm})…")
    errors = classifier.prefetch(recipes, workers=args.workers, refresh=args.refresh, progress=progress)
    if errors:
        print(f"\n{len(errors)} recipe(s) failed and will be skipped:", file=sys.stderr)
        for name, exc in errors.items():
            print(f"  {name}: {exc}", file=sys.stderr)
    ok = [r for r in recipes if r.path.name not in errors]

    report = Pipeline(manager, [classifier]).run(ok)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_review(classifier, report.results, tagging=diet_uids is not None), encoding="utf-8")

    counts = Counter(c.diet for c in classifier.classifications.values())
    status = Counter(r.status.value for r in report.results)
    print("\n" + "  ".join(f"{DIET_NAMES[d]}: {counts.get(d, 0)}" for d in DIETS))
    print(f"Needs review (not tagged): {status.get('skipped', 0)}   Errors: {len(errors) + status.get('error', 0)}")
    verb = "updated" if args.apply else "would change"
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {status.get('modified', 0)} recipe file(s) {verb}")
    print(f"Review: {args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
