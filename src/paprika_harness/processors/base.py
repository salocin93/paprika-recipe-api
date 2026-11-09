"""Extensible processor interface for the recipe pipeline."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from ..file_manager import RecipeFileManager, WriteResult
from ..recipe import Recipe

log = logging.getLogger(__name__)


class Status(str, Enum):
    MODIFIED = "modified"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class ProcessResult:
    recipe: Recipe
    status: Status
    message: str = ""


@dataclass
class PipelineReport:
    results: list[ProcessResult] = field(default_factory=list)
    writes: list[WriteResult] = field(default_factory=list)

    @property
    def counts(self) -> Counter:
        return Counter(r.status.value for r in self.results)


class RecipeProcessor(ABC):
    """Base class for anything that inspects or modifies recipes.

    Subclasses implement :meth:`process`, mutating ``recipe.frontmatter`` /
    ``recipe.body`` in place as needed. The pipeline decides whether changes
    are written (dry-run vs apply) — processors never touch the filesystem.
    """

    name: str = "processor"

    def setup(self) -> None:
        """Called once before the first recipe (open connections, etc.)."""

    def teardown(self) -> None:
        """Called once after the last recipe."""

    def should_process(self, recipe: Recipe) -> bool:
        """Override to filter which recipes this processor handles."""
        return True

    @abstractmethod
    def process(self, recipe: Recipe) -> ProcessResult:
        """Process a single recipe and report what happened."""


class Pipeline:
    """Run one or more processors over every recipe managed by ``manager``."""

    def __init__(self, manager: RecipeFileManager, processors: list[RecipeProcessor]):
        self.manager = manager
        self.processors = processors

    def run(self, recipes: list[Recipe] | None = None) -> PipelineReport:
        recipes = recipes if recipes is not None else self.manager.scan().recipes
        report = PipelineReport()

        for p in self.processors:
            p.setup()
        try:
            for recipe in recipes:
                for p in self.processors:
                    report.results.append(self._run_one(p, recipe))
                if recipe.is_modified:
                    try:
                        report.writes.append(self.manager.save(recipe))
                    except Exception as exc:  # keep going; surface in report
                        log.error("Failed to save %s: %s", recipe.path.name, exc)
                        report.results.append(
                            ProcessResult(recipe, Status.ERROR, f"save failed: {exc}")
                        )
        finally:
            for p in self.processors:
                p.teardown()
        return report

    @staticmethod
    def _run_one(p: RecipeProcessor, recipe: Recipe) -> ProcessResult:
        if not p.should_process(recipe):
            return ProcessResult(recipe, Status.SKIPPED)
        try:
            return p.process(recipe)
        except Exception as exc:
            log.exception("%s failed on %s", p.name, recipe.path.name)
            return ProcessResult(recipe, Status.ERROR, str(exc))
