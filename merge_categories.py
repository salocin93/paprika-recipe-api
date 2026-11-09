#!/usr/bin/env python3
"""Merge or remove categories across all recipes, driven by a YAML plan.

Dry run by default — prints exactly which recipes would change. Pass --apply
to write (originals are backed up to --backup-dir first).

Only the local Markdown files change; categories in the Paprika app/cloud
are untouched.

Usage:
    uv run merge_categories.py [--plan category_merges.yaml] [--show-diff]
    uv run merge_categories.py --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from paprika_harness import CategoryMap, RecipeFileManager, load_settings
from paprika_harness.processors import Pipeline, Status
from paprika_harness.processors.category_merge import CategoryMerger, MergePlan, MergePlanError


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", type=Path, default=Path("category_merges.yaml"))
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--category-map", type=Path, default=settings.category_map_file)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    ap.add_argument("--show-diff", action="store_true", help="print raw file diffs")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        cmap = CategoryMap.load(args.category_map)
        if not len(cmap):
            print(f"error: no category map at {args.category_map}; run fetch_categories.py first", file=sys.stderr)
            return 2
        plan = MergePlan.load(args.plan, cmap)
        manager = RecipeFileManager(args.recipes_dir, dry_run=not args.apply, backup_dir=args.backup_dir)
    except (FileNotFoundError, NotADirectoryError, MergePlanError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    scan = manager.scan()
    if scan.errors:
        print(f"warning: {len(scan.errors)} file(s) could not be parsed and will be skipped", file=sys.stderr)

    # Show the plan with how many recipes each source currently affects.
    usage = Counter(uid for r in scan.recipes for uid in r.categories)
    print("Plan:")
    for rule in plan.rules:
        for s in rule.sources:
            print(f"  {cmap.display(s)} ({usage[s]})  →  {cmap.display(rule.target)} ({usage[rule.target]})")
    for uid in plan.remove:
        print(f"  {cmap.display(uid)} ({usage[uid]})  →  removed")
    print()

    report = Pipeline(manager, [CategoryMerger(plan, cmap)]).run(scan.recipes)
    changed = [r for r in report.results if r.status is Status.MODIFIED]
    errors = [r for r in report.results if r.status is Status.ERROR]

    title_counts = Counter(r.recipe.title for r in changed)
    for r in sorted(changed, key=lambda r: (r.recipe.title.casefold(), r.recipe.path.name)):
        label = r.recipe.title
        if title_counts[label] > 1:
            label += f" [{r.recipe.path.name}]"
        print(f"  {label}: {r.message}")
    if args.show_diff:
        for w in report.writes:
            print(w.diff)
    for r in errors:
        print(f"  ERROR {r.recipe.path.name}: {r.message}", file=sys.stderr)

    written = sum(1 for w in report.writes if w.written)
    if args.apply:
        print(f"\nAPPLIED: {written} recipe file(s) updated; backups in {args.backup_dir}/")
    else:
        print(f"\nDRY RUN: {len(changed)} recipe(s) would change. Re-run with --apply to write.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
