"""Dietary classification processor (placeholder — LLM logic comes later)."""

from __future__ import annotations

import logging
import re
from typing import Literal

from ..config import LLMConfig
from ..recipe import Recipe
from .base import ProcessResult, RecipeProcessor, Status

log = logging.getLogger(__name__)

DietLabel = Literal["vegan", "vegetarian", "meat"]


def extract_ingredients(body: str) -> list[str]:
    """Return the bullet lines under the ``## Ingredients`` heading."""
    match = re.search(r"^## Ingredients\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    if not match:
        return []
    return [
        line.lstrip("-* ").strip()
        for line in match.group(1).splitlines()
        if line.strip().startswith(("-", "*"))
    ]


class DietaryClassifier(RecipeProcessor):
    """Classify recipes as vegan / vegetarian / meat.

    **Stub.** Currently a pass-through: it prints what it *would* send to the
    LLM and leaves the recipe untouched. To implement, replace
    :meth:`classify` with a call to the OpenAI-compatible endpoint in
    ``self.llm`` and set ``write_field`` to persist the label.
    """

    name = "dietary_classifier"

    def __init__(self, llm: LLMConfig, *, write_field: str | None = None, verbose: bool = True):
        self.llm = llm
        self.write_field = write_field  # e.g. "diet"; None = never modify
        self.verbose = verbose

    def setup(self) -> None:
        target = f"{self.llm.base_url} (model={self.llm.model})" if self.llm.is_configured else "<LLM not configured>"
        log.info("DietaryClassifier stub active; LLM target: %s", target)

    def classify(self, title: str, ingredients: list[str]) -> DietLabel | None:
        """TODO: call the local LLM here. Returns None until implemented."""
        return None

    def process(self, recipe: Recipe) -> ProcessResult:
        ingredients = extract_ingredients(recipe.body)
        if self.verbose:
            preview = ", ".join(ingredients[:5]) + (" …" if len(ingredients) > 5 else "")
            print(f"[DietaryClassifier] would classify: {recipe.title!r} "
                  f"({len(ingredients)} ingredients: {preview})")

        label = self.classify(recipe.title, ingredients)
        if label is None or not self.write_field:
            return ProcessResult(recipe, Status.UNCHANGED, "stub: no classification")

        if recipe.frontmatter.get(self.write_field) == label:
            return ProcessResult(recipe, Status.UNCHANGED, label)
        recipe.frontmatter[self.write_field] = label
        return ProcessResult(recipe, Status.MODIFIED, label)
