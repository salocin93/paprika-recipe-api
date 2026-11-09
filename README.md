# paprika-recipe-api

A modular Python harness for processing recipes exported from
[Paprika](https://www.paprikaapp.com/) as Markdown files with YAML frontmatter.

## Setup

```bash
uv sync
cp .env.example .env   # then edit
```

Place (or point `RECIPES_DIR` at) your export. The `recipes/` directory is
git-ignored because it contains personal data.

## Scripts

| Script | What it does | Writes to recipes? |
|---|---|---|
| `uv run analyze_categories.py [--json]` | Category report → `reports/category_analysis.md` | Never |
| `uv run fetch_categories.py` | Downloads category UID → name map from your Paprika account into `category_map.yaml` | Never |
| `uv run merge_categories.py [--show-diff] [--apply]` | Merges/removes categories per `category_merges.yaml` | Only with `--apply` (backs up first) |
| `uv run classify_dietary.py [--limit N] [--apply]` | LLM diet classification → `By Diet › Vegan/Vegetarian/Pescatarian/Meat` | Only with `--apply` |

### Category names

Paprika's Markdown export stores **category UIDs**, not names. Without a
mapping, the report shows UIDs plus example recipes for each. To get names
you can either:

* set `PAPRIKA_EMAIL` / `PAPRIKA_PASSWORD` in `.env` and run
  `uv run fetch_categories.py`, or
* hand-write `category_map.yaml` as `{uid: name}`.

Name-based duplicate detection (case mismatches, plurals, similar spelling,
related meaning like *Baking* vs *Desserts*) only works on resolved names.
Overlap-based detection (when most recipes in category A are also in B)
works either way.

### Merging categories

```bash
cp category_merges.example.yaml category_merges.yaml   # edit it
uv run merge_categories.py            # dry run: lists every recipe that would change
uv run merge_categories.py --apply    # write; originals copied to backups/<timestamp>/
```

Reference categories by name, path (`By Type › Pasta`, or `By Type/Pasta`), a
path suffix (`Italian/Pasta`) or UID. The whole plan is validated before any
recipe is touched. Unknown or ambiguous names, a category that is both source
and target, or a category used in two rules all abort with the list of
problems. Only the local Markdown files change, not your Paprika account.

## Architecture

```
src/paprika_harness/
├── config.py          # .env loader (paths, LOCAL_LLM_* settings)
├── recipe.py          # Recipe dataclass with change tracking
├── file_manager.py    # scan / parse / safe write (dry-run default, atomic, backups)
├── categories.py      # UID → name map + Paprika API fetcher
├── analytics.py       # category analysis + Markdown/console/JSON rendering
├── llm.py             # OpenAI-compatible client: structured JSON, rate limiting
├── diet.py            # diet prompt, keyword cross-check, result cache
└── processors/
    ├── base.py        # RecipeProcessor ABC + Pipeline runner
    ├── category_merge.py  # MergePlan + CategoryMerger
    └── dietary.py     # DietaryClassifier: tags By Diet categories
```

### Safe writes

* `RecipeFileManager(dir)` is **dry-run by default**. `save()` returns a
  unified diff and touches nothing unless constructed with `dry_run=False`.
* Untouched files are never rewritten. A body-only change keeps the original
  frontmatter bytes. A frontmatter change is re-serialised with the same
  PyYAML settings as the exporter (a test checks the real export round-trips
  byte for byte).
* Writes are atomic (temp file + `os.replace`), preserve LF/CRLF and BOM,
  are validated by re-parsing first, and can be backed up to `--backup-dir`.
* Malformed files (no frontmatter, unclosed block, bad YAML, non-mapping,
  invalid UTF-8) are collected in `ScanResult.errors` rather than crashing
  the run.

### Writing a processor

```python
from paprika_harness.processors import RecipeProcessor, ProcessResult, Status

class AddSourceTag(RecipeProcessor):
    name = "add_source_tag"

    def should_process(self, recipe):
        return "source" in recipe.frontmatter

    def process(self, recipe):
        recipe.frontmatter["source_domain"] = recipe.frontmatter["source"].lower()
        return ProcessResult(recipe, Status.MODIFIED)
```

Run it with `Pipeline(RecipeFileManager(dir), [AddSourceTag()]).run()`.

### Diet classification

`classify_dietary.py` sends each recipe's title, ingredients, directions and
notes to the LLM configured in `.env` (any OpenAI-compatible endpoint: LM
Studio, Ollama, vLLM, Gemini, …). The LLM picks the most restrictive label
the recipe satisfies **as written**: vegan, vegetarian, pescatarian or meat.

Setup: in the Paprika app, create a `By Diet` category with children
`Vegan`, `Vegetarian`, `Pescatarian` and `Meat`, then run
`uv run fetch_categories.py`. (Without them the script only classifies.)

```bash
uv run classify_dietary.py            # classify everything, write reports/diet_review.md
uv run classify_dietary.py --apply    # tag recipes (uses cached results, no new LLM calls)
```

* **Cache:** results are stored in `reports/diet_classifications.json`, keyed
  by a fingerprint of the recipe text, model and prompt version. Only new or
  changed recipes hit the LLM. Use `--refresh` to force re-classification.
* **Safety net:** an independent keyword scan (English + German) checks each
  answer. A recipe is **not tagged** if the LLM's label is more permissive
  than the keywords show (e.g. "vegetarian" for a recipe listing chicken),
  if confidence is below `--min-confidence` (0.75), or if the recipe has no
  ingredient list. These go to the "Needs review" table.
  `--include-flagged` tags them anyway.
* **Tagging** replaces any existing `By Diet` child and leaves all other
  categories alone.
* **Rate limits:** set `LOCAL_LLM_MAX_RPM` (or `--rpm`) to stay under the
  provider's limit. Gemini's free tier allows 15 requests per minute.

## Tests

```bash
uv run pytest
```
