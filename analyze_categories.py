#!/usr/bin/env python3
"""Step 1: structural analysis of recipe categories (read-only).

Usage:
    uv run analyze_categories.py [--recipes-dir PATH] [--category-map PATH]
                                 [--out reports/category_analysis.md] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from paprika_harness import CategoryMap, RecipeFileManager, load_settings
from paprika_harness.analytics import analyze_categories, render_console, render_markdown, to_json


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--category-map", type=Path, default=settings.category_map_file)
    ap.add_argument("--out", type=Path, default=Path("reports/category_analysis.md"))
    ap.add_argument("--json", action="store_true", help="also write a .json report next to --out")
    ap.add_argument("--fuzzy-threshold", type=float, default=0.8)
    ap.add_argument("--overlap-threshold", type=float, default=0.75)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    try:
        manager = RecipeFileManager(args.recipes_dir)  # read-only usage; dry_run default
        category_map = CategoryMap.load(args.category_map)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    scan = manager.scan()
    if not scan.recipes:
        print(f"error: no parseable recipes found in {args.recipes_dir}", file=sys.stderr)
        return 1

    report = analyze_categories(
        scan, category_map,
        fuzzy_threshold=args.fuzzy_threshold,
        overlap_threshold=args.overlap_threshold,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_markdown(report), encoding="utf-8")
    if args.json:
        args.out.with_suffix(".json").write_text(
            json.dumps(to_json(report), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(render_console(report))
    print(f"\nFull report: {args.out}" + (f" (+ {args.out.with_suffix('.json')})" if args.json else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
