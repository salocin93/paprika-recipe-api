from pathlib import Path

import pytest

from paprika_harness.file_manager import (
    FrontmatterError,
    RecipeFileManager,
    split_frontmatter,
)

SAMPLE = """---
categories:
- AAA
cook_time: ''
rating: 0
scale: null
source_url: https://example.com/a-very-long-url-that-goes-on-and-on-and-on-and-on/with?query=1&r=recipes/group
  recipes
uid: X1
---

# Pasta

## Ingredients

- 300 g pasta
"""


def write(tmp_path: Path, name: str, text: str, newline: str = "\n") -> Path:
    p = tmp_path / name
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return p


def test_split_is_lossless():
    head, fm, body = split_frontmatter(SAMPLE)
    assert head + body == SAMPLE
    assert body.startswith("\n# Pasta")
    assert fm.endswith("uid: X1\n")


def test_load_parses_fields(tmp_path):
    write(tmp_path, "a.md", SAMPLE)
    r = RecipeFileManager(tmp_path).load(tmp_path / "a.md")
    assert r.categories == ["AAA"]
    assert r.title == "Pasta"
    assert r.frontmatter["scale"] is None
    assert not r.is_modified


def test_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RecipeFileManager(tmp_path / "nope")


@pytest.mark.parametrize(
    "text, msg",
    [
        ("# no frontmatter\n", "does not start"),
        ("---\nuid: 1\n# never closed\n", "not closed"),
        ("---\nuid: [1, 2\n---\nbody\n", "malformed YAML"),
        ("---\n- a list\n---\nbody\n", "must be a mapping"),
    ],
)
def test_malformed_files_are_reported_not_raised(tmp_path, text, msg):
    write(tmp_path, "good.md", SAMPLE)
    write(tmp_path, "bad.md", text)
    result = RecipeFileManager(tmp_path).scan()
    assert [r.path.name for r in result.recipes] == ["good.md"]
    assert len(result.errors) == 1 and msg in result.errors[0].reason


def test_invalid_utf8_reported(tmp_path):
    (tmp_path / "bin.md").write_bytes(b"---\nuid: \xff\n---\n")
    result = RecipeFileManager(tmp_path).scan()
    assert "UTF-8" in result.errors[0].reason


def test_delimiter_inside_value_not_mistaken_for_closing(tmp_path):
    text = "---\nnotes: '---not a delimiter'\nuid: 1\n---\nbody ---\n"
    _, fm, body = split_frontmatter(text)
    assert "uid: 1" in fm and body == "body ---\n"


def test_dry_run_is_default_and_does_not_write(tmp_path):
    p = write(tmp_path, "a.md", SAMPLE)
    fm = RecipeFileManager(tmp_path)
    assert fm.dry_run is True
    r = fm.load(p)
    r.categories = ["AAA", "BBB"]
    res = fm.save(r)
    assert res.written is False and "+- BBB" in res.diff
    assert p.read_text() == SAMPLE


def test_apply_writes_minimal_diff_and_preserves_format(tmp_path):
    p = write(tmp_path, "a.md", SAMPLE)
    fm = RecipeFileManager(tmp_path, dry_run=False, backup_dir=tmp_path / "bk")
    r = fm.load(p)
    r.frontmatter["rating"] = 5
    res = fm.save(r)
    assert res.written and res.backup_path.read_text() == SAMPLE
    assert p.read_text() == SAMPLE.replace("rating: 0", "rating: 5")
    assert not r.is_modified  # snapshot reset after save


def test_body_only_change_keeps_frontmatter_bytes(tmp_path):
    p = write(tmp_path, "a.md", SAMPLE, newline="\r\n")
    fm = RecipeFileManager(tmp_path, dry_run=False)
    r = fm.load(p)
    r.body = r.body + "Extra line\r\n"
    fm.save(r)
    assert p.read_bytes() == (SAMPLE.replace("\n", "\r\n") + "Extra line\r\n").encode()


def test_crlf_preserved_on_frontmatter_change(tmp_path):
    p = write(tmp_path, "a.md", SAMPLE, newline="\r\n")
    fm = RecipeFileManager(tmp_path, dry_run=False)
    r = fm.load(p)
    r.frontmatter["rating"] = 3
    fm.save(r)
    data = p.read_bytes()
    assert b"rating: 3\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_unmodified_save_is_noop(tmp_path):
    p = write(tmp_path, "a.md", SAMPLE)
    fm = RecipeFileManager(tmp_path, dry_run=False)
    assert fm.save(fm.load(p)).written is False


REAL_RECIPES = Path(__file__).resolve().parents[1] / "recipes"


@pytest.mark.skipif(not REAL_RECIPES.is_dir(), reason="no local recipe export")
def test_real_export_round_trips_byte_for_byte():
    """Re-dumping every real recipe's frontmatter must reproduce the file exactly."""
    from paprika_harness.file_manager import dump_frontmatter

    scan = RecipeFileManager(REAL_RECIPES).scan()
    assert not scan.errors
    for r in scan.recipes:
        _, fm_text, _ = split_frontmatter(r.raw_text)
        assert dump_frontmatter(r.frontmatter) == fm_text, r.path.name


def test_backups_from_one_run_share_a_folder(tmp_path, monkeypatch):
    import paprika_harness.file_manager as fmod
    for n in ("a", "b"):
        write(tmp_path, f"{n}.md", SAMPLE)
    stamps = iter(["20260101-000000", "20260101-000001"])

    class FakeDT:
        @staticmethod
        def now():
            class T:
                def strftime(self, _):
                    return next(stamps)
            return T()

    monkeypatch.setattr(fmod, "datetime", FakeDT)
    fm = RecipeFileManager(tmp_path, dry_run=False, backup_dir=tmp_path / "bk")
    for r in fm.scan().recipes:
        r.frontmatter["rating"] = 1
        fm.save(r)
    assert [p.name for p in (tmp_path / "bk").iterdir()] == ["20260101-000000"]
