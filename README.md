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
| `uv run classify_dietary.py [--limit N] [--apply]` | Runs the (stub) `DietaryClassifier` | Only with `--apply` |

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

## Architecture

```
src/paprika_harness/
├── config.py          # .env loader (paths, LOCAL_LLM_* settings)
├── recipe.py          # Recipe dataclass with change tracking
├── file_manager.py    # scan / parse / safe write (dry-run default, atomic, backups)
├── categories.py      # UID → name map + Paprika API fetcher
├── analytics.py       # category analysis + Markdown/console/JSON rendering
└── processors/
    ├── base.py        # RecipeProcessor ABC + Pipeline runner
    └── dietary.py     # DietaryClassifier stub (LLM goes here)
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

### Dietary classifier

`DietaryClassifier` currently prints what it would send to the LLM
(the title and the ingredients parsed from the `## Ingredients` section) and
changes nothing. To implement it, fill in `DietaryClassifier.classify()` with
a call to the OpenAI-compatible endpoint from `LOCAL_LLM_BASE_URL` /
`LOCAL_LLM_MODEL`, and pass `write_field="diet"` to persist labels.

## Tests

```bash
uv run pytest
```
