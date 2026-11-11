#!/usr/bin/env python3
"""Convert US-only units in your Paprika account (ingredients, directions, notes).

Runs the same conversion as convert_units.py, but on each recipe's text as it
is in Paprika now, so nothing else you changed in the app is overwritten.

Dry run by default. With --apply, the server's copy of every recipe is saved
to backups/paprika-<timestamp>/ first, and each upload is read back.

Usage:
    uv run push_units.py                          # dry run
    uv run push_units.py --only "Pad Kra Pao" --apply
    uv run push_units.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from paprika_harness import load_settings
from paprika_harness.paprika_api import PaprikaClient, PaprikaError
from paprika_harness.units import convert_line

FIELDS = ("ingredients", "directions", "notes")


def convert_fields(recipe: dict) -> tuple[dict, list[tuple[str, str]]]:
    """New values for the text fields that change, and the changed lines."""
    new, changes = {}, []
    for f in FIELDS:
        text = recipe.get(f) or ""
        lines = text.split("\n")
        out = [convert_line(line) for line in lines]
        if out != lines:
            new[f] = "\n".join(out)
            changes += [(a, b) for a, b in zip(lines, out) if a != b]
    return new, changes


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="+", metavar="TEXT", help="only recipes whose name contains any of these")
    ap.add_argument("--limit", type=int, help="push at most N recipes")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--show", action="store_true", help="print every changed line")
    ap.add_argument("--apply", action="store_true", help="upload changes (default: dry run)")
    ap.add_argument("--backup-dir", type=Path, default=Path("backups"))
    args = ap.parse_args(argv)

    if not (settings.paprika_email and settings.paprika_password):
        print("error: set PAPRIKA_EMAIL and PAPRIKA_PASSWORD in .env", file=sys.stderr)
        return 2
    client = PaprikaClient(settings.paprika_email, settings.paprika_password)

    def fetch(uid):
        try:
            return client.get_recipe(uid), None
        except PaprikaError as exc:
            return None, f"{uid}: {exc}"

    try:
        uids = sorted(client.list_recipes())
    except PaprikaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Reading {len(uids)} recipes from Paprika…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        fetched = list(pool.map(fetch, uids))
    errors = [e for _, e in fetched if e]

    needles = [n.casefold() for n in args.only or []]
    todo = []
    for server, _ in fetched:
        if not server or server.get("in_trash"):
            continue
        if needles and not any(n in (server.get("name") or "").casefold() for n in needles):
            continue
        new, changes = convert_fields(server)
        if new:
            todo.append((server, new, changes))
    todo.sort(key=lambda t: (t[0].get("name") or "").casefold())
    todo = todo[: args.limit]

    for server, _, changes in todo:
        print(f"  {(server.get('name') or '')[:60]}: {len(changes)} line(s)")
        if args.show or args.only:
            for a, b in changes:
                print(f"      - {a[:110]}\n      + {b[:110]}")

    failed = 0
    if args.apply and todo:
        dest = args.backup_dir / f"paprika-{datetime.now():%Y%m%d-%H%M%S}"
        dest.mkdir(parents=True, exist_ok=True)
        for i, (server, new, _) in enumerate(todo, 1):
            (dest / f"{server['uid']}.json").write_text(json.dumps(server, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                sent = client.update_recipe(server, **new)
                back = client.get_recipe(server["uid"])
                if back.get("hash") != sent["hash"] or any(back.get(f) != sent.get(f) for f in FIELDS):
                    raise PaprikaError("read-back does not match what was sent")
            except PaprikaError as exc:
                failed += 1
                print(f"  [{i}/{len(todo)}] FAILED {server.get('name', '')[:50]}: {exc}", file=sys.stderr)
            else:
                print(f"  [{i}/{len(todo)}] pushed {server.get('name', '')[:60]}", flush=True)
        print(f"Server copies saved to {dest}")

    for e in errors:
        print(f"  could not read {e}", file=sys.stderr)
    n = sum(len(c) for _, _, c in todo)
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: {n} line(s) in {len(todo) - failed} recipe(s) "
          f"{'pushed' if args.apply else 'would be pushed'}, {failed} failed, {len(errors)} unreadable")
    return 1 if failed or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
