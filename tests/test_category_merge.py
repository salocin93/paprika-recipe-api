import pytest

from paprika_harness.categories import CategoryMap
from paprika_harness.file_manager import RecipeFileManager
from paprika_harness.processors import CategoryMerger, MergePlan, MergePlanError, Pipeline
from paprika_harness.processors.category_merge import apply_mapping

MAP = """
- {uid: T, name: By Type, parent_uid: null}
- {uid: TP, name: Pasta, parent_uid: T}
- {uid: TD, name: Deserts, parent_uid: T}
- {uid: R, name: By Region, parent_uid: null}
- {uid: I, name: Italian, parent_uid: R}
- {uid: IP, name: Pasta, parent_uid: I}
- {uid: D, name: Dessert, parent_uid: null}
- {uid: G, name: Grill, parent_uid: null}
"""


@pytest.fixture
def cmap(tmp_path):
    p = tmp_path / "map.yaml"
    p.write_text(MAP)
    return CategoryMap.load(p)


def test_resolve(cmap):
    assert cmap.resolve("dessert") == "D"
    assert cmap.resolve("By Type › Pasta") == "TP"
    assert cmap.resolve("italian/pasta") == "IP"
    assert cmap.resolve("By Region > Italian > Pasta") == "IP"
    assert cmap.resolve("TP") == "TP"
    with pytest.raises(KeyError, match="ambiguous"):
        cmap.resolve("Pasta")
    with pytest.raises(KeyError, match="no category"):
        cmap.resolve("Nope")


def test_apply_mapping_dedupes_and_keeps_order():
    assert apply_mapping(["A", "D", "TD", "G"], {"D": "TD", "G": None}) == ["A", "TD"]


@pytest.mark.parametrize(
    "plan, msg",
    [
        ({"merges": [{"from": ["Pasta"], "to": "Deserts"}]}, "ambiguous"),
        ({"merges": [{"from": "Dessert"}]}, "needs 'from' and 'to'"),
        ({"merges": [{"from": ["Dessert", "Deserts"], "to": "Deserts"}]}, "both source and target"),
        ({"merges": [{"from": ["Dessert"], "to": "Deserts"}], "remove": ["Dessert"]}, "appears twice"),
        ({"merges": [{"from": ["Grill"], "to": "Dessert"}, {"from": ["Dessert"], "to": "Deserts"}]}, "chain"),
        ({"merge": []}, "unknown top-level key"),
        ({}, "empty"),
    ],
)
def test_invalid_plans(cmap, plan, msg):
    with pytest.raises(MergePlanError, match=msg):
        MergePlan.from_dict(plan, cmap)


def _recipe(tmp_path, name, cats):
    body = "".join(f"- {c}\n" for c in cats) if cats else ""
    head = "categories:\n" + body if cats else "categories: []\n"
    (tmp_path / f"{name}.md").write_text(f"---\n{head}uid: {name}\n---\n\n# {name}\n")


def test_merge_pipeline_dry_run_then_apply(tmp_path, cmap):
    rdir = tmp_path / "recipes"
    rdir.mkdir()
    _recipe(rdir, "Cake", ["D", "G"])
    _recipe(rdir, "Pie", ["D", "TD"])
    _recipe(rdir, "Soup", ["IP"])
    plan = MergePlan.from_dict(
        {"merges": [{"from": ["Dessert"], "to": "By Type › Deserts"}], "remove": ["Grill"]}, cmap
    )
    originals = {p.name: p.read_text() for p in rdir.iterdir()}

    report = Pipeline(RecipeFileManager(rdir), [CategoryMerger(plan, cmap)]).run()
    assert report.counts == {"modified": 2, "skipped": 1}
    assert {p.name: p.read_text() for p in rdir.iterdir()} == originals  # dry run

    fm = RecipeFileManager(rdir, dry_run=False, backup_dir=tmp_path / "bk")
    Pipeline(fm, [CategoryMerger(plan, cmap)]).run()
    loaded = {r.title: r.categories for r in fm.scan().recipes}
    assert loaded == {"Cake": ["TD"], "Pie": ["TD"], "Soup": ["IP"]}
    assert (rdir / "Soup.md").read_text() == originals["Soup.md"]
    assert len(list((tmp_path / "bk").rglob("*.md"))) == 2


def test_orphan_uid_allowed_as_source_not_target(cmap):
    orphan = "A79D2374-2236-4918-8C6E-C645AED8F0F5"
    plan = MergePlan.from_dict({"merges": [{"from": [orphan], "to": "Deserts"}]}, cmap)
    assert plan.mapping == {orphan: "TD"}
    assert MergePlan.from_dict({"remove": [orphan]}, cmap).mapping == {orphan: None}
    with pytest.raises(MergePlanError, match="no category"):
        MergePlan.from_dict({"merges": [{"from": ["Dessert"], "to": orphan}]}, cmap)
    with pytest.raises(MergePlanError, match="no category"):
        MergePlan.from_dict({"merges": [{"from": ["not-a-uid"], "to": "Deserts"}]}, cmap)
