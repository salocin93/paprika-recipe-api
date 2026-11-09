#!/usr/bin/env python3
"""Download category UID -> name mapping from your Paprika cloud account.

The Markdown export only contains category UIDs. This script reads
PAPRIKA_EMAIL / PAPRIKA_PASSWORD from .env and writes CATEGORY_MAP_FILE
(default: category_map.yaml), which analyze_categories.py picks up.

Usage:
    uv run fetch_categories.py [--out category_map.yaml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from paprika_harness import load_settings
from paprika_harness.categories import fetch_paprika_categories, write_category_map


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=settings.category_map_file)
    args = ap.parse_args(argv)

    if not (settings.paprika_email and settings.paprika_password):
        print("error: set PAPRIKA_EMAIL and PAPRIKA_PASSWORD in .env", file=sys.stderr)
        return 2
    try:
        categories = fetch_paprika_categories(settings.paprika_email, settings.paprika_password)
    except Exception as exc:
        print(f"error: could not fetch categories: {exc}", file=sys.stderr)
        return 1

    write_category_map(categories, args.out)
    print(f"Wrote {len(categories)} categories to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
