import pytest

from paprika_harness.categories import CategoryMap
from paprika_harness.diet import ClassificationCache, build_prompt, keyword_scan
from paprika_harness.file_manager import RecipeFileManager
from paprika_harness.llm import LLMError, extract_json
from paprika_harness.processors import DietaryClassifier, Pipeline
from paprika_harness.processors.dietary import resolve_diet_categories

MAP = """
- {uid: BD, name: By Diet, parent_uid: null}
- {uid: VG, name: Vegan, parent_uid: BD}
- {uid: VE, name: Vegetarian, parent_uid: BD}
- {uid: PE, name: Pescatarian, parent_uid: BD}
- {uid: ME, name: Meat, parent_uid: BD}
- {uid: OLDVEG, name: Vegetarian, parent_uid: null}
- {uid: X, name: Pasta, parent_uid: null}
"""


class FakeLLM:
    model = "fake-1"

    def __init__(self, answers):
        self.answers = answers  # title -> (diet, confidence)
        self.calls = 0

    def complete_json(self, system, user, schema, *, name="result"):
        self.calls += 1
        title = user.splitlines()[0].removeprefix("Recipe: ")
        diet, conf = self.answers[title]
        return {"diet": diet, "confidence": conf, "animal_ingredients": [], "reason": "fake"}


def recipe(dirpath, title, ingredients, cats=()):
    head = "categories:\n" + "".join(f"- {c}\n" for c in cats) if cats else "categories: []\n"
    ing = "".join(f"- {i}\n" for i in ingredients)
    (dirpath / f"{title}.md").write_text(
        f"---\n{head}uid: {title}\n---\n\n# {title}\n\n## Ingredients\n\n{ing}\n## Directions\n\nCook it.\n"
    )


@pytest.fixture
def cmap(tmp_path):
    p = tmp_path / "map.yaml"
    p.write_text(MAP)
    return CategoryMap.load(p)


@pytest.mark.parametrize(
    "ingredients, floor",
    [
        (["1 eggplant", "2 tbsp peanut butter", "coconut milk", "vegetable stock"], "vegan"),
        (["2 eggs", "butter"], "vegetarian"),
        (["1 tbsp fish sauce", "200 ml cream"], "pescatarian"),
        (["500 g Hähnchenbrust", "Lachs"], "meat"),
        (["zest and rind of 1 lemon", "honeydew melon", "butterflied tofu"], "vegan"),
        (["200 g Speck"], "meat"),
        (["Rahmspargel"], "vegetarian"),
    ],
)
def test_keyword_scan(ingredients, floor):
    assert keyword_scan(ingredients).floor == floor


def test_extract_json_lenient():
    assert extract_json('Sure!\n```json\n{"diet": "vegan"}\n```') == {"diet": "vegan"}
    assert extract_json('blah {"a": 1} blah') == {"a": 1}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_prompt_includes_sections(tmp_path):
    recipe(tmp_path, "Soup", ["1 onion"])
    r = RecipeFileManager(tmp_path).scan().recipes[0]
    p = build_prompt(r)
    assert "Recipe: Soup" in p and "- 1 onion" in p and "Cook it." in p


def test_resolve_diet_categories(cmap, tmp_path):
    assert resolve_diet_categories(cmap) == {"vegan": "VG", "vegetarian": "VE", "pescatarian": "PE", "meat": "ME"}
    with pytest.raises(KeyError, match="By Food › Vegan"):
        resolve_diet_categories(cmap, "By Food")


def test_classify_tag_review_and_cache(tmp_path, cmap):
    rdir = tmp_path / "r"
    rdir.mkdir()
    recipe(rdir, "Salad", ["lettuce", "olive oil"], cats=["X", "ME"])      # wrong diet tag -> replaced
    recipe(rdir, "Omelette", ["3 eggs"], cats=["VE"])                      # already correct
    recipe(rdir, "Chicken Pie", ["500 g chicken"])                          # LLM wrong -> conflict
    recipe(rdir, "Fishy", ["fish sauce"])                                   # low confidence
    llm = FakeLLM({"Salad": ("vegan", 0.95), "Omelette": ("vegetarian", 0.9),
                   "Chicken Pie": ("vegetarian", 0.99), "Fishy": ("pescatarian", 0.5)})
    cache = ClassificationCache(tmp_path / "cache.json")
    uids = resolve_diet_categories(cmap)

    fm = RecipeFileManager(rdir)
    clf = DietaryClassifier(llm, cache, diet_uids=uids)
    assert clf.prefetch(fm.scan().recipes, workers=2) == {}
    assert llm.calls == 4
    report = Pipeline(fm, [clf]).run()
    by = {r.recipe.title: r for r in report.results}
    assert by["Salad"].status.value == "modified"
    assert by["Omelette"].status.value == "unchanged"
    assert by["Chicken Pie"].status.value == "skipped" and "keywords suggest meat" in by["Chicken Pie"].message
    assert by["Fishy"].status.value == "skipped" and "low confidence" in by["Fishy"].message
    assert "+- VG" in report.writes[0].diff and "-- ME" in report.writes[0].diff
    assert (rdir / "Salad.md").read_text().count("ME") == 1  # dry run: untouched

    # Second run: everything cached, apply writes.
    fm2 = RecipeFileManager(rdir, dry_run=False)
    clf2 = DietaryClassifier(llm, ClassificationCache(tmp_path / "cache.json"), diet_uids=uids)
    Pipeline(fm2, [clf2]).run()
    assert llm.calls == 4
    cats = {r.title: r.categories for r in fm2.scan().recipes}
    assert cats["Salad"] == ["X", "VG"] and cats["Omelette"] == ["VE"] and cats["Chicken Pie"] == []


def test_cache_invalidated_when_recipe_changes(tmp_path):
    recipe(tmp_path, "Soup", ["1 onion"])
    llm = FakeLLM({"Soup": ("vegan", 0.9)})
    clf = DietaryClassifier(llm, ClassificationCache(tmp_path / "c.json"))
    clf.classify(RecipeFileManager(tmp_path).scan().recipes[0])
    recipe(tmp_path, "Soup", ["1 onion", "bacon"])
    llm.answers["Soup"] = ("meat", 0.9)
    assert clf.classify(RecipeFileManager(tmp_path).scan().recipes[0]).diet == "meat"
    assert llm.calls == 2


def test_classify_only_mode_never_modifies(tmp_path):
    recipe(tmp_path, "Soup", ["1 onion"])
    clf = DietaryClassifier(FakeLLM({"Soup": ("vegan", 0.9)}), ClassificationCache(tmp_path / "c.json"))
    report = Pipeline(RecipeFileManager(tmp_path), [clf]).run()
    assert report.writes == [] and report.results[0].status.value == "unchanged"


def test_rate_limiter_spaces_calls(monkeypatch):
    from paprika_harness import llm as llm_mod
    clock = {"t": 100.0}
    sleeps = []
    monkeypatch.setattr(llm_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: sleeps.append(s))
    rl = llm_mod.RateLimiter(rpm=30)  # one call per 2 s
    for _ in range(3):
        rl.wait()
    assert sleeps == [2.0, 4.0]
    assert llm_mod.RateLimiter(None).interval == 0
