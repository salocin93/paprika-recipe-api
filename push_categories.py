#!/usr/bin/env python3
"""Push local category changes to your Paprika account.

Compares each local recipe with the untouched export (--baseline-dir) to see
which categories were added or removed locally, then applies exactly that
change to the recipe as it is in Paprika *now*. Everything else in the
recipe (name, ingredients, categories you changed in the app) is left as it
is on the server.

Dry run by default. With --apply, the server's copy of every recipe is saved
to backups/paprika-<timestamp>/ before it is changed, and each upload is
read back to check it.

Usage:
    uv run push_categories.py                     # dry run
    uv run push_categories.py --only "Focaccia" --apply
    uv run push_categories.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from paprika_harness import CategoryMap, RecipeFileManager, load_settings
from paprika_harness.paprika_api import PaprikaClient, PaprikaError, patch_categories


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipes-dir", type=Path, default=settings.recipes_dir)
    ap.add_argument("--baseline-dir", type=Path, default=Path("recipes_backup"),
                    help="untouched copy of the export the local changes started from")
    ap.add_argument("--category-map", type=Path, default=settings.category_map_file)
    ap.add_argument("--only", nargs="+", metavar="TEXT", help="only recipes whose title contains any of these")
    ap.add_argument("--limit", type=int, help="push at most N recipes")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--apply", action="store_true", help="upload changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    args = ap.parse_args(argv)

    if not (settings.paprika_email and settings.paprika_password):
        print("error: set PAPRIKA_EMAIL and PAPRIKA_PASSWORD in .env", file=sys.stderr)
        return 2
    try:
        local = {r.uid: r for r in RecipeFileManager(args.recipes_dir).scan().recipes if r.uid}
        baseline = {r.uid: r for r in RecipeFileManager(args.baseline_dir).scan().recipes if r.uid}
        cmap = CategoryMap.load(args.category_map)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    changed = [r for uid, r in local.items()
               if uid in baseline and set(r.categories) != set(baseline[uid].categories)]
    no_baseline = [r for uid, r in local.items() if uid not in baseline]
    if args.only:
        needles = [n.casefold() for n in args.only]
        changed = [r for r in changed if any(n in r.title.casefold() for n in needles)]
    changed.sort(key=lambda r: r.title.casefold())

    client = PaprikaClient(settings.paprika_email, settings.paprika_password)
    try:
        on_server = client.list_recipes()
    except PaprikaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def fetch(recipe):
        if recipe.uid not in on_server:
            return recipe, None, "not in the Paprika account"
        try:
            return recipe, client.get_recipe(recipe.uid), None
        except PaprikaError as exc:
            return recipe, None, str(exc)

    print(f"{len(changed)} recipe(s) changed locally; reading them from Paprika…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fetched = list(pool.map(fetch, changed))

    todo, skipped = [], []
    for recipe, server, err in fetched:
        if err:
            skipped.append((recipe, err))
        elif server.get("in_trash"):
            skipped.append((recipe, "in the trash in Paprika"))
        else:
            new = patch_categories(server.get("categories") or [], baseline[recipe.uid].categories, recipe.categories)
            if new != (server.get("categories") or []):
                todo.append((recipe, server, new))
    todo = todo[: args.limit]

    def names(uids):
        return ", ".join(sorted(cmap.display(u) for u in uids)) or "(none)"

    for recipe, server, new in todo:
        old = server.get("categories") or []
        plus, minus = set(new) - set(old), set(old) - set(new)
        name = server.get("name") or recipe.title
        print(f"  {name[:60]}" + (f"\n      + {names(plus)}" if plus else "") + (f"\n      - {names(minus)}" if minus else ""))

    failed = []
    if args.apply and todo:
        dest = args.backup_dir / f"paprika-{datetime.now():%Y%m%d-%H%M%S}"
        dest.mkdir(parents=True, exist_ok=True)
        for i, (recipe, server, new) in enumerate(todo, 1):
            (dest / f"{recipe.uid}.json").write_text(json.dumps(server, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                sent = client.set_categories(server, new)
                back = client.get_recipe(recipe.uid)
                if back.get("categories") != new or back.get("hash") != sent["hash"]:
                    raise PaprikaError("read-back does not match what was sent")
            except PaprikaError as exc:
                failed.append((recipe, str(exc)))
                print(f"  [{i}/{len(todo)}] FAILED {recipe.title[:50]}: {exc}", file=sys.stderr)
            else:
                print(f"  [{i}/{len(todo)}] pushed {recipe.title[:60]}", flush=True)
        print(f"Server copies saved to {dest}")

    for recipe, why in skipped:
        print(f"  skipped {recipe.title[:60]}: {why}")
    if no_baseline:
        print(f"note: {len(no_baseline)} local recipe(s) have no baseline copy and were ignored")
    done = len(todo) - len(failed)
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {done} recipe(s) {'pushed' if args.apply else 'would be pushed'}, "
          f"{len(skipped)} skipped, {len(failed)} failed, "
          f"{len(changed) - len(todo) - len(skipped) if not args.limit else '?'} already up to date")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
