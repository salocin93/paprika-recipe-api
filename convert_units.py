#!/usr/bin/env python3
"""Replace US-only units in recipes with rounded metric amounts.

Converts pounds, ounces, gallons/quarts/pints, inches and °F in the
Ingredients, Directions and Notes sections. Cups, tablespoons and teaspoons
are left alone. Where a line already gives a metric amount next to the US
one, the author's metric number is kept and the US one dropped. Ounces of a
liquid are converted as volume.

Dry run by default: writes every changed line to reports/unit_conversion.md.

Usage:
    uv run convert_units.py            # dry run + report
    uv run convert_units.py --apply    # write (originals copied to backups/)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from paprika_harness import RecipeFileManager, load_settings
from paprika_harness.processors import Pipeline, ProcessResult, RecipeProcessor, Status
from paprika_harness.units import convert_body, needs_check


class UnitConverter(RecipeProcessor):
    name = "unit_converter"

    def __init__(self) -> None:
        self.changes: dict[str, tuple[str, list[tuple[str, str]]]] = {}

    def process(self, recipe) -> ProcessResult:
        body, changes = convert_body(recipe.body)
        if not changes:
            return ProcessResult(recipe, Status.UNCHANGED)
        self.changes[recipe.path.name] = (recipe.title, changes)
        recipe.body = body
        return ProcessResult(recipe, Status.MODIFIED, f"{len(changes)} line(s)")


def render(changes) -> str:
    check = [(t, b, a) for t, rows in changes.values() for b, a in rows if needs_check(b, a)]
    n = sum(len(rows) for _, rows in changes.values())
    out = ["# Unit conversion\n", f"{n} lines in {len(changes)} recipes.\n"]
    out.append(f"## Check these ({len(check)})\n")
    out.append("The line already had a metric amount that could not be paired with the US one.\n")
    out += [f"- **{t[:60]}**  \n  `{b}`  \n  `{a}`" for t, b, a in check] + [""]
    out.append("## All changes\n")
    for title, rows in sorted(changes.values(), key=lambda v: v[0].casefold()):
        out.append(f"**{title[:80]}**\n")
        out += [f"- `{b}`  \n  `{a}`" for b, a in rows] + [""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--only", nargs="+", metavar="TEXT", help="only recipes whose title contains any of these")
    ap.add_argument("--report", type=Path, default=Path("reports/unit_conversion.md"))
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    args = ap.parse_args(argv)

    try:
        manager = RecipeFileManager(args.recipes_dir, dry_run=not args.apply, backup_dir=args.backup_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    recipes = manager.scan().recipes
    if args.only:
        needles = [n.casefold() for n in args.only]
        recipes = [r for r in recipes if any(n in r.title.casefold() for n in needles)]

    converter = UnitConverter()
    report = Pipeline(manager, [converter]).run(recipes)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render(converter.changes), encoding="utf-8")
    errors = [r for r in report.results if r.status is Status.ERROR]
    n = sum(len(rows) for _, rows in converter.changes.values())
    verb = "updated" if args.apply else "would change"
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {n} line(s) in {len(converter.changes)} recipe file(s) {verb}; {len(errors)} error(s)")
    print(f"Report: {args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
