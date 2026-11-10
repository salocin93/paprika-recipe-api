import gzip
import json
from pathlib import Path

import pytest

from paprika_harness import CategoryMap, RecipeFileManager
from paprika_harness.paprika_api import PaprikaClient, patch_categories, recipe_hash
from paprika_harness.processors import Pipeline
from paprika_harness.processors.category_fill import CategoryFiller
from paprika_harness.taxonomy import SuggestionCache, load_branch
from paprika_harness.units import convert_line, find_us_units


# -- units ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "line, expected",
    [
        ("1 lb ground beef", "450 g ground beef"),
        ("1 1/2 pounds chicken", "675 g chicken"),
        ("2-1/2 pounds beef", "1.1 kg beef"),
        ("½ lb butter", "225 g butter"),
        ("2-3 lbs pork", "0.9–1.4 kg pork"),
        ("1 (14-ounce) can tomatoes", "1 (400 g) can tomatoes"),
        ("8 oz. cream cheese", "225 g cream cheese"),
        ("8 fl oz milk", "225 ml milk"),
        ("a 9x13-inch pan", "a 23 x 33 cm pan"),
        ("cut 1 inch thick", "cut 2.5 cm thick"),
        ("Preheat oven to 350°F.", "Preheat oven to 180°C."),
        ("2 quarts stock", "1.9 l stock"),
    ],
)
def test_us_units_convert_to_rounded_metric(line, expected):
    assert convert_line(line) == expected


@pytest.mark.parametrize("line", ["2 cups flour", "1 tbsp oil", "1 tsp salt", "500 g flour", "1 pinch", "in a bowl", "mix in a bowl"])
def test_cups_spoons_and_metric_are_left_alone(line):
    assert convert_line(line) == line and not find_us_units(line)


def test_ounces_of_liquid_are_flagged_as_uncertain():
    (hit,) = find_us_units("8 oz heavy cream")
    assert hit.uncertain and hit.proposed == "225 ml"


# -- pushing to Paprika --------------------------------------------------------
def test_patch_applies_only_the_local_change():
    # Local: removed B, added D. Server meanwhile gained X in the app.
    assert patch_categories(["A", "B", "X"], ["A", "B"], ["A", "D"]) == ["A", "X", "D"]
    assert patch_categories(["A", "D"], ["A"], ["A", "D"]) == ["A", "D"]  # already there


def test_set_categories_uploads_full_recipe_with_new_hash(monkeypatch):
    sent = {}

    def fake_request(self, path, data=None, headers=None):
        sent.update(path=path, data=data, headers=headers)
        return True

    monkeypatch.setattr(PaprikaClient, "_request", fake_request)
    server = {"uid": "U1", "name": "Soup", "notes": "keep me", "categories": ["A"], "hash": "OLD"}
    new = PaprikaClient("e", "p").set_categories(server, ["A", "B"])

    assert server["categories"] == ["A"]  # input not mutated
    assert new["hash"] == recipe_hash(new) != "OLD"
    assert sent["path"] == "recipe/U1/" and "multipart/form-data" in sent["headers"]["Content-Type"]
    gz = sent["data"].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    assert json.loads(gzip.decompress(gz)) == new and new["notes"] == "keep me"


# -- filling missing categories ------------------------------------------------
CMAP = CategoryMap(
    names={"T": "By Type", "T1": "Soups", "T2": "Salad", "R": "By Region", "R1": "Asian", "R2": "Thai"},
    parents={"T1": "T", "T2": "T", "R1": "R", "R2": "R1"},
)
RECIPE = "---\ncategories:{cats}\nuid: {uid}\n---\n\n# {title}\n\n## Ingredients\n\n- 1 thing\n"


class FakeLLM:
    model = "fake"

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def complete_json(self, system, user, schema, *, name="x"):
        self.calls += 1
        assert schema["properties"]["region"]["enum"] == ["Asian", "Asian › Thai", "none"]
        return self.answer


def make(tmp_path: Path, uid: str, cats: list[str]):
    text = RECIPE.format(uid=uid, title=f"Recipe {uid}", cats="".join(f"\n- {c}" for c in cats) or " []")
    (tmp_path / f"{uid}.md").write_text(text)


def run(tmp_path, answer, **kw):
    branches = [load_branch(CMAP, "type", "By Type"), load_branch(CMAP, "region", "By Region")]
    llm = FakeLLM(answer)
    filler = CategoryFiller(llm, SuggestionCache(tmp_path / "cache.json"), branches, **kw)
    manager = RecipeFileManager(tmp_path, dry_run=False)
    Pipeline(manager, [filler]).run()
    return {r.uid: r.categories for r in manager.scan().recipes}, llm, filler


def test_only_missing_branches_are_filled(tmp_path):
    make(tmp_path, "a", [])
    make(tmp_path, "b", ["T2"])        # has a type already
    make(tmp_path, "c", ["T1", "R2"])  # complete: no LLM call
    answer = {"type": "Soups", "type_confidence": 0.9, "region": "Thai", "region_confidence": 0.95, "reason": "r"}
    cats, llm, _ = run(tmp_path, answer)
    assert cats == {"a": ["T1", "R2"], "b": ["T2", "R2"], "c": ["T1", "R2"]}
    assert llm.calls == 2


def test_low_confidence_and_none_are_not_applied(tmp_path):
    make(tmp_path, "a", [])
    answer = {"type": "Salad", "type_confidence": 0.5, "region": "none", "region_confidence": 0.9, "reason": "r"}
    cats, _, filler = run(tmp_path, answer)
    assert cats == {"a": []}
    assert [o[3] for o in filler.outcomes["a.md"]] == ["review", "none"]
