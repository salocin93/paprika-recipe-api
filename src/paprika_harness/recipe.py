"""In-memory representation of a single recipe file."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Recipe:
    """A parsed recipe: YAML frontmatter + Markdown body.

    ``frontmatter`` and ``body`` are free to mutate. The original values are
    snapshotted at load time so the file manager can tell whether anything
    actually changed and skip rewriting untouched files.
    """

    path: Path
    frontmatter: dict[str, Any]
    body: str
    raw_text: str = field(repr=False)
    _orig_frontmatter: dict[str, Any] = field(init=False, repr=False)
    _orig_body: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._orig_frontmatter = copy.deepcopy(self.frontmatter)
        self._orig_body = self.body

    # -- convenience accessors -------------------------------------------------
    @property
    def uid(self) -> str | None:
        return self.frontmatter.get("uid")

    @property
    def title(self) -> str:
        """Title from the first ``# `` heading, falling back to the filename."""
        for line in self.body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return self.path.stem

    @property
    def categories(self) -> list[str]:
        value = self.frontmatter.get("categories") or []
        if isinstance(value, str):  # tolerate a scalar instead of a list
            value = [value]
        return [str(v) for v in value if v is not None and str(v).strip()]

    @categories.setter
    def categories(self, values: list[str]) -> None:
        self.frontmatter["categories"] = list(values)

    # -- change tracking ------------------------------------------------------
    @property
    def frontmatter_changed(self) -> bool:
        return self.frontmatter != self._orig_frontmatter

    @property
    def body_changed(self) -> bool:
        return self.body != self._orig_body

    @property
    def is_modified(self) -> bool:
        return self.frontmatter_changed or self.body_changed

    def mark_saved(self, new_raw_text: str) -> None:
        self.raw_text = new_raw_text
        self.__post_init__()


@dataclass(frozen=True)
class ParseError:
    """A file that could not be loaded, with the reason."""

    path: Path
    reason: str
