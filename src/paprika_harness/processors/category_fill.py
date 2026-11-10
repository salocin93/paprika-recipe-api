"""Fill in missing "By Type" / "By Region" categories from LLM suggestions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..diet import LLMLike
from ..recipe import Recipe
from ..taxonomy import NONE, Branch, Suggestion, SuggestionCache, suggest
from .base import ProcessResult, RecipeProcessor, Status


class CategoryFiller(RecipeProcessor):
    """Add one category per branch to recipes that have none in that branch.

    A branch a recipe is already filed under is never touched. A suggestion is
    only applied when its confidence is at least ``min_confidence``; the rest
    are left for review (see ``outcomes``).
    """

    name = "category_filler"

    def __init__(self, llm: LLMLike, cache: SuggestionCache, branches: list[Branch], *, min_confidence: float = 0.8):
        self.llm = llm
        self.cache = cache
        self.branches = branches
        self.min_confidence = min_confidence
        # filename -> list of (branch key, choice, confidence, action); action is
        # "add", "none" (LLM found no fit) or "review" (low confidence)
        self.outcomes: dict[str, list[tuple[str, str, float, str]]] = {}
        self.suggestions: dict[str, Suggestion] = {}

    def missing(self, recipe: Recipe) -> list[Branch]:
        cats = set(recipe.categories)
        return [b for b in self.branches if not cats & b.members]

    def should_process(self, recipe: Recipe) -> bool:
        return bool(self.missing(recipe))

    def get(self, recipe: Recipe, *, refresh: bool = False) -> Suggestion:
        cached = None if refresh else self.cache.get(recipe, self.llm.model, self.branches)
        if cached:
            return cached
        result = suggest(recipe, self.llm, self.branches)
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
        todo = [r for r in recipes if self.should_process(r)
                and (refresh or not self.cache.get(r, self.llm.model, self.branches))]
        errors: dict[str, Exception] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.get, r, refresh=refresh): r for r in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                recipe, exc = futures[fut], fut.exception()
                if exc:
                    errors[recipe.path.name] = exc
                if progress:
                    progress(i, len(todo), recipe, exc)
        return errors

    def process(self, recipe: Recipe) -> ProcessResult:
        s = self.get(recipe)
        self.suggestions[recipe.path.name] = s
        outcome, added = [], []
        for b in self.missing(recipe):
            choice, conf = s.choices.get(b.key, NONE), s.confidence.get(b.key, 0.0)
            if choice == NONE:
                action = "none"
            elif conf < self.min_confidence:
                action = "review"
            else:
                action = "add"
                added.append(b.options[choice])
            outcome.append((b.key, choice, conf, action))
        self.outcomes[recipe.path.name] = outcome
        if not added:
            return ProcessResult(recipe, Status.SKIPPED if any(o[3] == "review" for o in outcome) else Status.UNCHANGED)
        recipe.categories = recipe.categories + added
        return ProcessResult(recipe, Status.MODIFIED, ", ".join(o[1] for o in outcome if o[3] == "add"))
