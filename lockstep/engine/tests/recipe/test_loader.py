from pathlib import Path

import pytest
import yaml

from lockstep.recipe.loader import RecipeError, RecipeLoader


def write_recipe(path: Path, *, name: str, generated: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"name": name, "nodes": {}}
    if generated:
        doc["x-lockstep-generated"] = {"source": "test"}
    path.write_text(yaml.safe_dump(doc))


def test_exact_suffix_defines_logical_name(tmp_path):
    path = tmp_path / ".lockstep/recipes/release.recipe.yaml"
    write_recipe(path, name="release")
    assert RecipeLoader(path.parent).resolve("release").name == "release"


def test_path_stem_is_not_used(tmp_path):
    write_recipe(tmp_path / "release.yaml", name="release")
    with pytest.raises(RecipeError, match=".recipe.yaml"):
        RecipeLoader(tmp_path).resolve("release")


def test_loader_rejects_document_name_mismatch(tmp_path):
    path = tmp_path / "release.recipe.yaml"
    write_recipe(path, name="other")

    with pytest.raises(RecipeError, match="document name"):
        RecipeLoader(tmp_path).discover()


def test_loader_marks_mapping_metadata_as_generated(tmp_path):
    path = tmp_path / "release.recipe.yaml"
    write_recipe(path, name="release", generated=True)

    ref = RecipeLoader(tmp_path).resolve(path)

    assert ref.kind == "generated"
    assert RecipeLoader(tmp_path).load(ref)["name"] == "release"
