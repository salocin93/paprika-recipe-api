"""Dietary classification processor: tags recipes with a "By Diet" category."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..categories import CategoryMap
from ..diet import DIETS, Classification, ClassificationCache, LLMLike, classify_recipe
from ..diet import extract_ingredients  # noqa: F401  (re-export for backwards compat)
from ..recipe import Recipe
from .base import ProcessResult, RecipeProcessor, Status

log = logging.getLogger(__name__)

DIET_NAMES = {"vegan": "Vegan", "vegetarian": "Vegetarian", "pescatarian": "Pescatarian", "meat": "Meat"}


def resolve_diet_categories(cmap: CategoryMap, parent: str = "By Diet") -> dict[str, str]:
    """Map each diet label to the UID of ``<parent> › <Name>``. Raises KeyError if any is missing."""
    missing, uids = [], {}
    for diet in DIETS:
        path = f"{parent} › {DIET_NAMES[diet]}"
        try:
            uids[diet] = cmap.resolve(path)
        except KeyError:
            missing.append(path)
    if missing:
        raise KeyError("missing categories in Paprika: " + ", ".join(missing))
    return uids


class DietaryClassifier(RecipeProcessor):
    """Classify each recipe as vegan / vegetarian / pescatarian / meat.

    Args:
        llm: client with ``complete_json`` (see :class:`paprika_harness.llm.LLMClient`).
        cache: results are stored here so re-runs don't call the LLM again.
        diet_uids: label -> category UID. ``None`` = classify only, never modify.
        min_confidence: below this, the recipe is reported for review, not tagged.
        include_flagged: also tag recipes that were flagged for review.
    """

    name = "dietary_classifier"

    def __init__(
        self,
        llm: LLMLike,
        cache: ClassificationCache,
        *,
        diet_uids: dict[str, str] | None = None,
        min_confidence: float = 0.75,
        include_flagged: bool = False,
    ):
        self.llm = llm
        self.cache = cache
        self.diet_uids = diet_uids
        self.min_confidence = min_confidence
        self.include_flagged = include_flagged
        self.classifications: dict[str, Classification] = {}

    # -- classification --------------------------------------------------------
    def classify(self, recipe: Recipe, *, refresh: bool = False) -> Classification:
        cached = None if refresh else self.cache.get(recipe, self.llm.model)
        if cached:
            return cached
        result = classify_recipe(recipe, self.llm)
        self.cache.put(result)
        return result

    def prefetch(
        self,
        recipes: Iterable[Recipe],
        *,
        workers: int = 4,
        refresh: bool = False,
        progress: Callable[[int, int, Recipe, Exception | None], None] | None = None,
    ) -> dict[str, Exception]:
        """Classify many recipes concurrently, filling the cache. Returns errors by filename."""
        todo = [r for r in recipes if refresh or not self.cache.get(r, self.llm.model)]
        errors: dict[str, Exception] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.classify, r, refresh=refresh): r for r in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                recipe, exc = futures[fut], fut.exception()
                if exc:
                    errors[recipe.path.name] = exc
                if progress:
                    progress(i, len(todo), recipe, exc)
        return errors

    # -- RecipeProcessor -------------------------------------------------------
    def process(self, recipe: Recipe) -> ProcessResult:
        c = self.classify(recipe)
        self.classifications[recipe.path.name] = c

        review = c.needs_review(self.min_confidence)
        if review and not self.include_flagged:
            return ProcessResult(recipe, Status.SKIPPED, f"review: {'; '.join(review)}")
        if self.diet_uids is None:
            return ProcessResult(recipe, Status.UNCHANGED, f"classified {c.diet} (not tagging)")

        target = self.diet_uids[c.diet]
        diet_set = set(self.diet_uids.values())
        before = recipe.categories
        after = [u for u in before if u not in diet_set] + [target]
        if after == before or (target in before and set(after) == set(before)):
            return ProcessResult(recipe, Status.UNCHANGED, c.diet)
        recipe.categories = after
        return ProcessResult(recipe, Status.MODIFIED, c.diet)
