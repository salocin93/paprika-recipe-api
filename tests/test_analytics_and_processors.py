from pathlib import Path

from paprika_harness.analytics import analyze_categories, normalize, render_markdown
from paprika_harness.categories import CategoryMap
from paprika_harness.config import LLMConfig
from paprika_harness.file_manager import RecipeFileManager
from paprika_harness.processors import DietaryClassifier, Pipeline, RecipeProcessor, Status


def make(tmp_path: Path, name: str, cats: list[str], ingredients=("1 egg",)) -> None:
    cat_yaml = "categories: []\n" if not cats else "categories:\n" + "".join(f"- {c}\n" for c in cats)
    ing = "".join(f"- {i}\n" for i in ingredients)
    (tmp_path / f"{name}.md").write_text(
        f"---\n{cat_yaml}uid: {name}\n---\n\n# {name}\n\n## Ingredients\n\n{ing}\n## Directions\n\nCook.\n"
    )


def test_normalize():
    assert normalize("Desserts") == normalize("dessert")
    assert normalize("Cookies & Bars") == normalize("cookie and bar")
    assert normalize("Berries") == normalize("berry")
    assert normalize("Side Dishes") == normalize("side dish")
    assert normalize("Grass") == "grass"


def test_semantic_group_matches_plural_names(tmp_path):
    make(tmp_path, "A", ["c1"])
    make(tmp_path, "B", ["c2"])
    m = tmp_path / "m.yaml"
    m.write_text("c1: Cookies\nc2: Desserts\n")
    report = analyze_categories(RecipeFileManager(tmp_path).scan(), CategoryMap.load(m))
    assert report.similar and "related meaning" in report.similar[0].reason


def test_category_report(tmp_path):
    make(tmp_path, "Cake", ["u1", "u2"])
    make(tmp_path, "Pie", ["u1", "u2"])
    make(tmp_path, "Tart", ["u1", "u2", "u4"])
    make(tmp_path, "Soup", ["u3"])
    make(tmp_path, "Toast", [])
    (tmp_path / "broken.md").write_text("no frontmatter")
    cmap = tmp_path / "map.yaml"
    cmap.write_text("u1: Baking\nu2: Desserts\nu3: soups\nu4: Soup\nu5: Unused\n")

    scan = RecipeFileManager(tmp_path).scan()
    report = analyze_categories(scan, CategoryMap.load(cmap))

    counts = {report.category_map.name(u): len(rs) for u, rs in report.recipes_by_category.items()}
    assert counts == {"Baking": 3, "Desserts": 3, "soups": 1, "Soup": 1, "Unused": 0}
    assert [r.title for r in report.uncategorized] == ["Toast"]
    assert len(report.errors) == 1

    reasons = {frozenset((report.category_map.name(p.a), report.category_map.name(p.b))): p.reason
               for p in report.similar}
    assert "related meaning" in reasons[frozenset({"Baking", "Desserts"})]
    assert "100%" in reasons[frozenset({"Baking", "Desserts"})]
    assert "normalising" in reasons[frozenset({"soups", "Soup"})]

    md = render_markdown(report)
    assert "Recipes with no category (1)" in md and "Unused" in md


def test_category_map_api_shape(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("- {uid: a, name: Mains, parent_uid: null}\n- {uid: b, name: Pasta, parent_uid: a}\n")
    cm = CategoryMap.load(p)
    assert cm.display("b") == "Mains › Pasta"
    assert cm.name("zzz") == "zzz" and not cm.is_resolved("zzz")


def test_missing_category_map_is_empty(tmp_path):
    assert len(CategoryMap.load(tmp_path / "nope.yaml")) == 0


def test_dietary_stub_is_passthrough(tmp_path, capsys):
    make(tmp_path, "Omelette", [], ingredients=["2 eggs", "butter"])
    fm = RecipeFileManager(tmp_path)
    before = (tmp_path / "Omelette.md").read_text()
    report = Pipeline(fm, [DietaryClassifier(LLMConfig(None, None, None))]).run()
    assert report.counts == {"unchanged": 1} and report.writes == []
    assert "would classify: 'Omelette' (2 ingredients: 2 eggs, butter)" in capsys.readouterr().out
    assert (tmp_path / "Omelette.md").read_text() == before


def test_pipeline_dry_run_and_error_isolation(tmp_path):
    make(tmp_path, "A", [])
    make(tmp_path, "B", [])

    class Tagger(RecipeProcessor):
        def process(self, recipe):
            if recipe.title == "B":
                raise RuntimeError("boom")
            recipe.frontmatter["diet"] = "vegetarian"
            from paprika_harness.processors import ProcessResult
            return ProcessResult(recipe, Status.MODIFIED)

    before = (tmp_path / "A.md").read_text()
    report = Pipeline(RecipeFileManager(tmp_path), [Tagger()]).run()
    assert report.counts == {"modified": 1, "error": 1}
    assert len(report.writes) == 1 and "+diet: vegetarian" in report.writes[0].diff
    assert (tmp_path / "A.md").read_text() == before  # dry run


def test_hierarchy_rules(tmp_path):
    rows = """
- {uid: r, name: By Region, parent_uid: null}
- {uid: it, name: Italian, parent_uid: r}
- {uid: ip, name: Pasta, parent_uid: it}
- {uid: t, name: By Type, parent_uid: null}
- {uid: tp, name: Pasta, parent_uid: t}
- {uid: ja, name: Japanese, parent_uid: r}
- {uid: th, name: Thai, parent_uid: r}
- {uid: g, name: German, parent_uid: null}
- {uid: ga, name: German & Austrian, parent_uid: r}
"""
    m = tmp_path / "m.yaml"
    m.write_text(rows)
    for i in range(3):
        make(tmp_path, f"P{i}", ["it", "ip", "tp"])
    cm = CategoryMap.load(m)
    assert cm.display("ip") == "By Region › Italian › Pasta"
    report = analyze_categories(RecipeFileManager(tmp_path).scan(), cm)
    pairs = {frozenset((cm.display(p.a), cm.display(p.b))): p.reason for p in report.similar}
    # parent/child never flagged
    assert frozenset({"By Region › Italian", "By Region › Italian › Pasta"}) not in pairs
    assert "same name in different branches" in pairs[frozenset({"By Type › Pasta", "By Region › Italian › Pasta"})]
    assert "contains" in pairs[frozenset({"German", "By Region › German & Austrian"})]
    assert frozenset({"By Region › Japanese", "By Region › Thai"}) not in pairs
