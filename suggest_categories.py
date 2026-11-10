#!/usr/bin/env python3
"""Fill in missing "By Type" / "By Region" categories with an LLM.

Only recipes that have no category in a branch get one, chosen from the
categories that already exist in that branch. Recipes already filed under a
branch are left alone there.

Dry run by default. Suggestions are cached in reports/category_suggestions.json.
Review reports/category_suggestions.md, then re-run with --apply.

Usage:
    uv run suggest_categories.py            # suggest + report
    uv run suggest_categories.py --apply    # write categories (backs up first)
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from paprika_harness import CategoryMap, RecipeFileManager, load_settings
from paprika_harness.config import ConfigError
from paprika_harness.llm import LLMClient
from paprika_harness.processors import Pipeline
from paprika_harness.processors.category_fill import CategoryFiller
from paprika_harness.taxonomy import SuggestionCache, load_branch


def render(filler: CategoryFiller, roots: dict[str, str]) -> str:
    rows = {"add": [], "review": [], "none": []}
    for name, outcome in filler.outcomes.items():
        s = filler.suggestions[name]
        for key, choice, conf, action in outcome:
            rows[action].append((s.title, key, choice, conf, s.reason))
    for v in rows.values():
        v.sort(key=lambda r: (r[1], r[2].casefold(), r[0].casefold()))
    out = ["# Category suggestions\n",
           f"Model: `{filler.llm.model}`, min confidence {filler.min_confidence}\n",
           "| | Count |\n|---|---:|",
           f"| Will be added | {len(rows['add'])} |",
           f"| Needs review (low confidence, not added) | {len(rows['review'])} |",
           f"| No fitting category | {len(rows['none'])} |\n"]
    for action, title in (("review", "Needs review"), ("none", "No fitting category"), ("add", "Will be added")):
        out.append(f"## {title} ({len(rows[action])})\n")
        out.append("| Recipe | Branch | Suggested | Conf. | Reason |\n|---|---|---|---:|---|")
        for t, key, choice, conf, reason in rows[action]:
            out.append(f"| {t[:70]} | {roots[key]} | {choice} | {conf:.2f} | {reason} |".replace("\n", " "))
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--category-map", type=Path, default=settings.category_map_file)
    ap.add_argument("--type-root", default="By Type")
    ap.add_argument("--region-root", default="By Region")
    ap.add_argument("--limit", type=int, help="only the first N recipes that need a category")
    ap.add_argument("--only", nargs="+", metavar="TEXT", help="only recipes whose title contains any of these")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rpm", type=float, help="max LLM requests per minute (default: LOCAL_LLM_MAX_RPM)")
    ap.add_argument("--min-confidence", type=float, default=0.8)
    ap.add_argument("--refresh", action="store_true", help="ignore cached suggestions")
    ap.add_argument("--cache", type=Path, default=Path("reports/category_suggestions.json"))
    ap.add_argument("--report", type=Path, default=Path("reports/category_suggestions.md"))
    ap.add_argument("--apply", action="store_true", help="write categories (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    roots = {"type": args.type_root, "region": args.region_root}
    try:
        llm = LLMClient(settings.llm, max_rpm=args.rpm)
        cache = SuggestionCache(args.cache)
        cmap = CategoryMap.load(args.category_map)
        branches = [load_branch(cmap, key, root) for key, root in roots.items()]
        manager = RecipeFileManager(args.recipes_dir, dry_run=not args.apply, backup_dir=args.backup_dir)
    except (ConfigError, FileNotFoundError, NotADirectoryError, ValueError, KeyError) as exc:
        print(f"error: {exc.args[0] if isinstance(exc, KeyError) else exc}", file=sys.stderr)
        return 2

    filler = CategoryFiller(llm, cache, branches, min_confidence=args.min_confidence)
    recipes = [r for r in manager.scan().recipes if filler.should_process(r)]
    if args.only:
        needles = [n.casefold() for n in args.only]
        recipes = [r for r in recipes if any(n in r.title.casefold() for n in needles)]
    recipes = recipes[: args.limit]

    def progress(i, n, recipe, exc):
        s = None if exc else cache.get(recipe, llm.model, branches)
        status = f"ERROR {exc}" if exc else " / ".join(s.choices.values())
        print(f"  [{i}/{n}] {recipe.title[:50]}: {status}", flush=True)

    print(f"{len(recipes)} recipe(s) lack a {args.type_root} or {args.region_root} category; asking {llm.model}…")
    errors = filler.prefetch(recipes, workers=args.workers, refresh=args.refresh, progress=progress)
    for name, exc in errors.items():
        print(f"  failed: {name}: {exc}", file=sys.stderr)
    ok = [r for r in recipes if r.path.name not in errors]

    report = Pipeline(manager, [filler]).run(ok)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(filler, roots), encoding="utf-8")

    actions = Counter(o[3] for outcome in filler.outcomes.values() for o in outcome)
    status = Counter(r.status.value for r in report.results)
    print(f"\nCategories to add: {actions['add']}   Needs review: {actions['review']}   "
          f"No fit: {actions['none']}   Errors: {len(errors) + status.get('error', 0)}")
    verb = "updated" if args.apply else "would change"
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {status.get('modified', 0)} recipe file(s) {verb}")
    print(f"Review: {args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
