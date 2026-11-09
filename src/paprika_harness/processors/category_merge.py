"""Merge, rename-into, or remove categories on recipes.

A merge plan (YAML) looks like::

    merges:
      - from: [Dessert]                 # one or more source categories
        to: By Type › Deserts           # the category to keep
      - from: [Hauptspeise, Main Course]
        to: By Type › Other Mains
    remove:
      - Grill                           # strip from every recipe

Categories can be referenced by name, path (or path suffix) or UID — see
:meth:`CategoryMap.resolve`. The plan is validated up front: unknown or
ambiguous references, a category that is both a source and a target, or a
source listed in two rules all abort before any recipe is touched.

Only the ``categories`` list in each recipe's frontmatter changes. The
category definitions themselves live in your Paprika account and are not
modified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..categories import CategoryMap
from ..recipe import Recipe
from .base import ProcessResult, RecipeProcessor, Status


class MergePlanError(ValueError):
    """The merge plan is invalid; message lists every problem found."""


@dataclass
class MergeRule:
    sources: list[str]  # uids
    target: str  # uid


@dataclass
class MergePlan:
    rules: list[MergeRule] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)  # uids

    @property
    def mapping(self) -> dict[str, str | None]:
        """source uid -> target uid (``None`` = remove)."""
        m: dict[str, str | None] = {s: r.target for r in self.rules for s in r.sources}
        m.update({u: None for u in self.remove})
        return m

    @classmethod
    def from_dict(cls, data: dict, cmap: CategoryMap) -> "MergePlan":
        if not isinstance(data, dict):
            raise MergePlanError("plan must be a mapping with 'merges' and/or 'remove'")
        unknown_keys = set(data) - {"merges", "remove"}
        problems: list[str] = [f"unknown top-level key: {k!r}" for k in sorted(unknown_keys)]

        def res(ref, where: str) -> str | None:
            try:
                return cmap.resolve(ref)
            except KeyError as exc:
                problems.append(f"{where}: {exc.args[0]}")
                return None

        plan = cls()
        for i, raw in enumerate(data.get("merges") or [], 1):
            where = f"merges[{i}]"
            if not isinstance(raw, dict) or "from" not in raw or "to" not in raw:
                problems.append(f"{where}: needs 'from' and 'to'")
                continue
            srcs = raw["from"] if isinstance(raw["from"], list) else [raw["from"]]
            target = res(raw["to"], f"{where}.to")
            sources = [u for u in (res(s, f"{where}.from") for s in srcs) if u]
            if target and target in sources:
                problems.append(f"{where}: {cmap.display(target)!r} is both source and target")
                sources = [s for s in sources if s != target]
            if target and sources:
                plan.rules.append(MergeRule(sources, target))
        for i, ref in enumerate(data.get("remove") or [], 1):
            uid = res(ref, f"remove[{i}]")
            if uid:
                plan.remove.append(uid)

        # Cross-rule consistency.
        seen: dict[str, str] = {}
        all_sources = [(s, f"merge → {cmap.display(r.target)}") for r in plan.rules for s in r.sources]
        all_sources += [(u, "remove") for u in plan.remove]
        for uid, ctx in all_sources:
            if uid in seen:
                problems.append(f"{cmap.display(uid)!r} appears twice ({seen[uid]} and {ctx})")
            seen[uid] = ctx
        targets = {r.target for r in plan.rules}
        for uid in targets & set(seen):
            problems.append(
                f"{cmap.display(uid)!r} is a merge target but is also merged/removed elsewhere "
                "(chain the rule directly to the final category instead)"
            )

        if problems:
            raise MergePlanError("Invalid merge plan:\n  - " + "\n  - ".join(problems))
        if not plan.rules and not plan.remove:
            raise MergePlanError("Merge plan is empty")
        return plan

    @classmethod
    def load(cls, path: Path, cmap: CategoryMap) -> "MergePlan":
        if not path.exists():
            raise FileNotFoundError(f"Merge plan not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise MergePlanError(f"Could not parse {path}: {exc}") from exc
        return cls.from_dict(data or {}, cmap)


def apply_mapping(categories: list[str], mapping: dict[str, str | None]) -> list[str]:
    """Replace/remove categories, de-duplicating while keeping first-seen order."""
    out: list[str] = []
    for uid in categories:
        new = mapping.get(uid, uid)
        if new is not None and new not in out:
            out.append(new)
    return out


class CategoryMerger(RecipeProcessor):
    name = "category_merger"

    def __init__(self, plan: MergePlan, cmap: CategoryMap):
        self.plan = plan
        self.cmap = cmap
        self.mapping = plan.mapping

    def should_process(self, recipe: Recipe) -> bool:
        return any(uid in self.mapping for uid in recipe.categories)

    def process(self, recipe: Recipe) -> ProcessResult:
        before = recipe.categories
        after = apply_mapping(before, self.mapping)
        if after == before:
            return ProcessResult(recipe, Status.UNCHANGED)
        recipe.categories = after
        removed = [self.cmap.display(u) for u in before if u not in after]
        added = [self.cmap.display(u) for u in after if u not in before]
        msg = "; ".join(
            part for part in (
                ("- " + ", ".join(removed)) if removed else "",
                ("+ " + ", ".join(added)) if added else "",
            ) if part
        )
        return ProcessResult(recipe, Status.MODIFIED, msg)
