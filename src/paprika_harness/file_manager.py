"""Scanning, parsing and (safely) writing recipe Markdown files.

Format handled::

    ---
    <YAML mapping>
    ---
    <Markdown body>

Writes are designed not to disturb anything you didn't change:

* If only the body changed, the original frontmatter text is reused verbatim.
* If the frontmatter changed it is re-serialised with the same PyYAML settings
  the Paprika exporter uses (verified to round-trip the export byte-for-byte).
* Line endings (LF / CRLF) and a leading BOM are preserved.
* Files are written atomically (temp file + rename), optionally with a backup.
* **Dry run is the default**: nothing touches disk unless ``dry_run=False``.
"""

from __future__ import annotations

import difflib
import logging
import os
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .recipe import ParseError, Recipe

log = logging.getLogger(__name__)

BOM = "﻿"
DELIM = "---"


class FrontmatterError(ValueError):
    """The file has missing or malformed YAML frontmatter."""


@dataclass
class ScanResult:
    recipes: list[Recipe] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.recipes) + len(self.errors)


@dataclass(frozen=True)
class WriteResult:
    path: Path
    written: bool
    dry_run: bool
    diff: str = ""
    backup_path: Path | None = None


# --------------------------------------------------------------------------- #
# Parsing / serialisation (pure functions, easy to test)
# --------------------------------------------------------------------------- #
def split_frontmatter(text: str) -> tuple[str, str, str]:
    """Split ``text`` into ``(head, frontmatter_yaml, body)``.

    ``head`` is everything up to and including the closing ``---`` line, so
    ``head + body == text`` always holds (lossless).
    """
    newline = "\r\n" if "\r\n" in text[:200] else "\n"
    start = 1 if text.startswith(BOM) else 0
    if not text.startswith(DELIM + newline, start):
        raise FrontmatterError("file does not start with a '---' frontmatter block")

    fm_start = start + len(DELIM) + len(newline)
    closing = newline + DELIM
    search_from = fm_start - len(newline)  # allow an empty frontmatter block
    while True:
        idx = text.find(closing, search_from)
        if idx == -1:
            raise FrontmatterError("frontmatter block is not closed with '---'")
        end = idx + len(closing)
        # The delimiter must occupy the whole line.
        if end == len(text) or text.startswith(newline, end):
            break
        search_from = end

    head_end = end + len(newline) if end < len(text) else end
    fm_text = text[fm_start : idx + len(newline)] if idx >= fm_start else ""
    return text[:head_end], fm_text, text[head_end:]


def parse_frontmatter(fm_text: str) -> dict:
    try:
        data = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 2})" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise FrontmatterError(f"malformed YAML{where}: {problem}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"frontmatter must be a mapping, got {type(data).__name__}"
        )
    return data


def dump_frontmatter(data: dict, newline: str = "\n") -> str:
    text = yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return text.replace("\n", newline) if newline != "\n" else text


def serialize(recipe: Recipe) -> str:
    """Render a recipe back to text, reusing original bytes wherever possible."""
    if not recipe.is_modified:
        return recipe.raw_text

    orig_head, _, _ = split_frontmatter(recipe.raw_text)
    if recipe.frontmatter_changed:
        newline = "\r\n" if "\r\n" in orig_head else "\n"
        bom = BOM if recipe.raw_text.startswith(BOM) else ""
        head = f"{bom}{DELIM}{newline}{dump_frontmatter(recipe.frontmatter, newline)}{DELIM}{newline}"
    else:
        head = orig_head
    return head + recipe.body


# --------------------------------------------------------------------------- #
# File manager
# --------------------------------------------------------------------------- #
class RecipeFileManager:
    """Load recipes from a directory and write modifications back safely.

    Args:
        recipes_dir: Directory containing ``*.md`` recipe files.
        dry_run: When True (the default) writes are simulated and only a diff
            is produced.
        backup_dir: If set (and not in dry-run), each file is copied here
            before being overwritten.
    """

    def __init__(
        self,
        recipes_dir: Path | str,
        *,
        dry_run: bool = True,
        backup_dir: Path | str | None = None,
        pattern: str = "*.md",
    ) -> None:
        self.recipes_dir = Path(recipes_dir).expanduser()
        self.dry_run = dry_run
        self.backup_dir = Path(backup_dir).expanduser() if backup_dir else None
        self.pattern = pattern
        self._backup_stamp: str | None = None  # one backup folder per manager/run

        if not self.recipes_dir.exists():
            raise FileNotFoundError(f"Recipes directory not found: {self.recipes_dir}")
        if not self.recipes_dir.is_dir():
            raise NotADirectoryError(f"Not a directory: {self.recipes_dir}")

    # -- reading ---------------------------------------------------------------
    def iter_paths(self) -> Iterator[Path]:
        for path in sorted(self.recipes_dir.glob(self.pattern)):
            if path.is_file() and not path.name.startswith("."):
                yield path

    def load(self, path: Path | str) -> Recipe:
        """Load one recipe. Raises ``FileNotFoundError`` / ``FrontmatterError``."""
        path = Path(path)
        # Decode bytes ourselves: read_text() would translate CRLF -> LF.
        try:
            raw = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrontmatterError(f"not valid UTF-8: {exc}") from exc
        _, fm_text, body = split_frontmatter(raw)
        return Recipe(path=path, frontmatter=parse_frontmatter(fm_text), body=body, raw_text=raw)

    def scan(self) -> ScanResult:
        """Load every recipe, collecting (not raising) per-file errors."""
        result = ScanResult()
        for path in self.iter_paths():
            try:
                result.recipes.append(self.load(path))
            except (FrontmatterError, OSError) as exc:
                log.warning("Skipping %s: %s", path.name, exc)
                result.errors.append(ParseError(path=path, reason=str(exc)))
        return result

    def __iter__(self) -> Iterator[Recipe]:
        return iter(self.scan().recipes)

    # -- writing ---------------------------------------------------------------
    def save(self, recipe: Recipe) -> WriteResult:
        """Persist ``recipe`` (or, in dry-run, just report what would change)."""
        if not recipe.is_modified:
            return WriteResult(recipe.path, written=False, dry_run=self.dry_run)

        new_text = serialize(recipe)
        # Validate before writing: the output must parse back to the same data.
        _, fm_check, body_check = split_frontmatter(new_text)
        if parse_frontmatter(fm_check) != recipe.frontmatter or body_check != recipe.body:
            raise FrontmatterError(f"round-trip validation failed for {recipe.path.name}")

        diff = "".join(
            difflib.unified_diff(
                recipe.raw_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{recipe.path.name}",
                tofile=f"b/{recipe.path.name}",
            )
        )

        if self.dry_run:
            log.info("[dry-run] would update %s", recipe.path.name)
            return WriteResult(recipe.path, written=False, dry_run=True, diff=diff)

        backup_path = self._backup(recipe.path) if self.backup_dir else None
        self._atomic_write(recipe.path, new_text)
        recipe.mark_saved(new_text)
        log.info("Updated %s", recipe.path.name)
        return WriteResult(recipe.path, written=True, dry_run=False, diff=diff, backup_path=backup_path)

    def save_all(self, recipes: list[Recipe]) -> list[WriteResult]:
        return [self.save(r) for r in recipes if r.is_modified]

    def _backup(self, path: Path) -> Path:
        assert self.backup_dir is not None
        if self._backup_stamp is None:
            self._backup_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_dir = self.backup_dir / self._backup_stamp
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        shutil.copy2(path, dest)
        return dest

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
