from pathlib import Path

import pytest
import yaml

from lockstep.recipe.loader import RecipeError, RecipeLoader


def write_recipe(path: Path, *, name: str, generated: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"name": name, "nodes": {}, "edges": []}
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


def test_loader_rejects_duplicate_logical_names(tmp_path):
    write_recipe(tmp_path / "one" / "release.recipe.yaml", name="release")
    write_recipe(tmp_path / "two" / "release.recipe.yaml", name="release")

    with pytest.raises(RecipeError, match="duplicate recipe name"):
        RecipeLoader(tmp_path).discover()


def test_loader_discovers_nested_public_recipes(tmp_path):
    write_recipe(tmp_path / "nested" / "review.recipe.yaml", name="review")
    write_recipe(tmp_path / "generated" / "other" / "release.recipe.yaml", name="release")

    assert sorted(RecipeLoader(tmp_path).discover()) == ["release", "review"]


def test_discovery_validates_dependencies_without_listing_call_site_specializations(tmp_path):
    child = tmp_path / "generated" / "children" / "call-site" / "review.recipe.yaml"
    write_recipe(child, name="review-specialized")
    (tmp_path / "release.recipe.yaml").write_text(yaml.safe_dump({
        "name": "release",
        "nodes": {"review": {
            "type": "subgraph", "graph": "generated/children/call-site/review.recipe.yaml",
            "mode": "direct",
        }},
        "edges": [],
    }))
    loader = RecipeLoader(tmp_path)
    assert sorted(loader.discover()) == ["release"]

    child.write_text("name: review\nname: hidden\nnodes: {}\nedges: []\n")
    with pytest.raises(RecipeError, match="duplicate mapping key"):
        loader.discover()


def test_loader_rejects_symlink_that_escapes_recipe_root(tmp_path):
    outside = tmp_path / "outside" / "release.recipe.yaml"
    write_recipe(outside, name="release")
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "release.recipe.yaml").symlink_to(outside)

    with pytest.raises(RecipeError, match="linked recipe input rejected"):
        RecipeLoader(recipes).discover()


def test_loader_marks_mapping_metadata_as_generated(tmp_path):
    path = tmp_path / "release.recipe.yaml"
    write_recipe(path, name="release", generated=True)

    ref = RecipeLoader(tmp_path).resolve(path)

    assert ref.kind == "generated"
    assert RecipeLoader(tmp_path).load(ref)["name"] == "release"


def test_recipe_loader_uses_the_single_strict_ingress(tmp_path):
    path = tmp_path / "release.recipe.yaml"
    path.write_text("name: release\nname: hidden\nnodes: {}\nedges: []\n")

    with pytest.raises(RecipeError, match="duplicate mapping key"):
        RecipeLoader(tmp_path).discover()


def test_recipe_reference_binds_the_complete_definition_digest(tmp_path):
    path = tmp_path / "release.recipe.yaml"
    write_recipe(path, name="release")
    loader = RecipeLoader(tmp_path)
    ref = loader.resolve("release")
    path.write_text(
        path.read_text().replace("edges: []", "edges:\n- {from: START, to: END}")
    )

    with pytest.raises(RecipeError, match="changed while loading"):
        loader.load(ref)
