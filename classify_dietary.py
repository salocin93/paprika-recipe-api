#!/usr/bin/env python3
"""Run the (stub) DietaryClassifier over all recipes.

Dry run by default: nothing is written unless --apply is passed.

Usage:
    uv run classify_dietary.py [--limit N] [--apply] [--backup-dir backups/]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from paprika_harness import RecipeFileManager, load_settings
from paprika_harness.processors import DietaryClassifier, Pipeline


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--limit", type=int, default=None, help="only process the first N recipes")
    ap.add_argument("--apply", action="store_true", help="actually write changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    ap.add_argument("--show-diff", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        manager = RecipeFileManager(args.recipes_dir, dry_run=not args.apply, backup_dir=args.backup_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    recipes = manager.scan().recipes[: args.limit]
    report = Pipeline(manager, [DietaryClassifier(settings.llm)]).run(recipes)

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {dict(report.counts)}; "
          f"{len(report.writes)} file(s) {'written' if args.apply else 'would change'}")
    if args.show_diff:
        for w in report.writes:
            print(w.diff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
