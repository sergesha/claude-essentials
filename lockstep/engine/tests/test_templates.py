from __future__ import annotations

import os
from importlib import import_module, resources
from pathlib import Path

import pytest
import yaml

from lockstep.authoring import (
    AuthoringError,
    canonical_match,
    compile_project_source,
    diff_recipe,
    project_paths,
    write_compilation,
)
from lockstep.recipe.authority import StrictRecipeIngress


EXPECTED_BUNDLES = {
    "reviewed-change": {
        "files": {
            "template.yaml",
            "parent.workflow.yaml",
            "review.workflow.yaml",
        },
        "outputs": {
            "parent": "{name}",
            "review": "{name}-review",
        },
    },
    "parallel-review": {
        "files": {
            "template.yaml",
            "parent.workflow.yaml",
            "security-review.workflow.yaml",
            "architecture-review.workflow.yaml",
        },
        "outputs": {
            "parent": "{name}",
            "security-review": "{name}-security-review",
            "architecture-review": "{name}-architecture-review",
        },
    },
}


def _templates():
    return import_module("lockstep.templates")


def _install_template(template: str, name: str, project: Path):
    return _templates().install_template(
        template,
        name,
        project,
        state_dir=(project.parent / f"{project.name}-owner-state").resolve(),
    )


def test_catalog_is_discovered_from_exact_package_resource_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _templates().list_templates() == ("parallel-review", "reviewed-change")

    package_root = resources.files("lockstep.templates")
    bundles = {
        item.name
        for item in package_root.iterdir()
        if item.is_dir() and not item.name.startswith("__")
    }
    assert bundles == set(EXPECTED_BUNDLES)


@pytest.mark.parametrize("bundle_name", sorted(EXPECTED_BUNDLES))
def test_each_bundle_has_one_manifest_as_its_complete_role_map(bundle_name: str) -> None:
    package_root = resources.files("lockstep.templates")
    bundle = package_root.joinpath(bundle_name)
    observed_files = {
        item.name for item in bundle.iterdir() if item.is_file()
    }

    assert observed_files == EXPECTED_BUNDLES[bundle_name]["files"]
    manifest = yaml.safe_load(bundle.joinpath("template.yaml").read_text())
    assert manifest == {
        "template_version": "1",
        "outputs": EXPECTED_BUNDLES[bundle_name]["outputs"],
        "files": {
            role: f"{role}.workflow.yaml"
            for role in EXPECTED_BUNDLES[bundle_name]["outputs"]
        },
    }


def test_template_show_returns_exact_roles_outputs_sources_and_compile_order() -> None:
    shown = _templates().show_template("parallel-review", "release")

    assert shown.to_dict() == {
        "template": "parallel-review",
        "name": "release",
        "roles": {
            "parent": "release",
            "security-review": "release-security-review",
            "architecture-review": "release-architecture-review",
        },
        "sources": {
            "parent": "parent.workflow.yaml",
            "security-review": "security-review.workflow.yaml",
            "architecture-review": "architecture-review.workflow.yaml",
        },
        "dependencies": {
            "release": ["release-security-review", "release-architecture-review"],
            "release-security-review": [],
            "release-architecture-review": [],
        },
        "compile_order": [
            "release-security-review",
            "release-architecture-review",
            "release",
        ],
    }


def test_template_show_ignores_call_shaped_metadata_without_reopening_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates = _templates()
    manifest = {
        "template_version": "1",
        "outputs": {"parent": "{name}", "review": "{name}-review"},
        "files": {
            "parent": "parent.workflow.yaml",
            "review": "review.workflow.yaml",
        },
    }
    reads = {name: 0 for name in manifest["files"].values()}
    contents = {
        "parent.workflow.yaml": """\
workflow_version: '1'
name: '{name}'
description: parent
protect: ['**']
x-shadow: {call: {workflow: '{name}'}}
flow:
  - call: {workflow: '{name}-review', runner: codex}
""",
        "review.workflow.yaml": """\
workflow_version: '1'
name: '{name}-review'
description: review
protect: ['**']
flow: [{escalate: {}}]
""",
    }

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

        def read_text(self) -> str:
            reads[self.name] += 1
            return contents[self.name]

    class Bundle:
        def joinpath(self, name: str) -> Entry:
            return Entry(name)

    monkeypatch.setattr(templates, "_manifest", lambda _name: manifest)
    monkeypatch.setattr(templates, "_bundle", lambda _name: Bundle())

    shown = templates.show_template("synthetic", "release")

    assert shown.dependencies == {
        "release": ["release-review"],
        "release-review": [],
    }
    assert shown.compile_order == ("release-review", "release")
    assert reads == {"parent.workflow.yaml": 1, "review.workflow.yaml": 1}


@pytest.mark.parametrize(
    "collision",
    [
        ".lockstep/workflows/release.workflow.yaml",
        ".lockstep/workflows/release-review.workflow.yaml",
        ".lockstep/recipes/release.recipe.yaml",
        ".lockstep/recipes/release-review.recipe.yaml",
    ],
)
def test_every_destination_is_preflighted_before_any_bundle_write(
    tmp_path: Path, collision: str
) -> None:
    occupied = tmp_path / collision
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_bytes(b"owner bytes\n")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(_templates().TemplateCollision, match=collision):
        _install_template("reviewed-change", "release", tmp_path)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_atomic_install_publishes_the_complete_self_contained_child_dag(
    tmp_path: Path,
) -> None:
    installed = _install_template("parallel-review", "release", tmp_path)

    expected_sources = {
        tmp_path / ".lockstep/workflows/release.workflow.yaml",
        tmp_path / ".lockstep/workflows/release-security-review.workflow.yaml",
        tmp_path / ".lockstep/workflows/release-architecture-review.workflow.yaml",
    }
    expected_recipes = {
        tmp_path / ".lockstep/recipes/release.recipe.yaml",
        tmp_path / ".lockstep/recipes/release-security-review.recipe.yaml",
        tmp_path / ".lockstep/recipes/release-architecture-review.recipe.yaml",
    }
    assert set(installed.sources) == expected_sources
    assert set(installed.recipes) == expected_recipes
    assert all(path.is_file() for path in expected_sources | expected_recipes)

    candidate = StrictRecipeIngress(tmp_path / ".lockstep/recipes").inspect(
        "release.recipe.yaml"
    )
    assert candidate.dependency_dag.root == "release.recipe.yaml"
    assert {item.path for item in candidate.files} >= {
        "release.recipe.yaml",
        "release-security-review.recipe.yaml",
        "release-architecture-review.recipe.yaml",
    }
    assert installed.compile_order == (
        "release-security-review",
        "release-architecture-review",
        "release",
    )


@pytest.mark.parametrize("bundle_name", sorted(EXPECTED_BUNDLES))
def test_template_install_compile_round_trip_preserves_canonical_child_dag(
    tmp_path: Path, bundle_name: str
) -> None:
    installed = _install_template(bundle_name, "release", tmp_path)
    expected_recipes = {
        f"{output.replace('{name}', 'release')}.recipe.yaml"
        for output in EXPECTED_BUNDLES[bundle_name]["outputs"].values()
    }

    for output in installed.compile_order:
        assert diff_recipe(tmp_path, output) == ""
        canonical_match(project_paths(tmp_path, output))
    before = StrictRecipeIngress(tmp_path / ".lockstep/recipes").inspect(
        "release.recipe.yaml"
    )
    assert {item.path for item in before.files} >= expected_recipes

    write_compilation(
        project_paths(tmp_path, "release"),
        state_dir=(tmp_path.parent / "template-owner-state").resolve(),
    )

    for output in installed.compile_order:
        assert diff_recipe(tmp_path, output) == ""
        canonical_match(project_paths(tmp_path, output))
    after = StrictRecipeIngress(tmp_path / ".lockstep/recipes").inspect(
        "release.recipe.yaml"
    )
    assert {item.path for item in after.files} >= expected_recipes


@pytest.mark.parametrize("bundle_name", sorted(EXPECTED_BUNDLES))
def test_unlinked_template_parent_recipe_is_not_canonical(
    tmp_path: Path, bundle_name: str
) -> None:
    _install_template(bundle_name, "release", tmp_path)
    recipe = project_paths(tmp_path, "release")
    _validated, _catalog, compiled = compile_project_source(recipe.workflow_path)
    recipe.recipe_path.write_bytes(compiled.recipe_bytes)

    with pytest.raises(AuthoringError, match="canonical match"):
        canonical_match(recipe)


def test_custom_template_path_is_rejected_as_a_v2_feature(tmp_path: Path) -> None:
    custom = tmp_path / "custom-template"
    custom.mkdir()

    with pytest.raises(ValueError, match="custom template paths are a v2 feature"):
        _install_template(str(custom), "release", tmp_path)


def test_compile_failure_before_publish_leaves_no_bundle_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = import_module("lockstep.authoring_installation")
    monkeypatch.setattr(
        installation,
        "compile_captured_source",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("compile fault")),
    )

    with pytest.raises(RuntimeError, match="compile fault"):
        _install_template("reviewed-change", "release", tmp_path)

    assert not (tmp_path / ".lockstep/workflows").exists()
    assert not (tmp_path / ".lockstep/recipes").exists()


def test_publish_fault_leaves_completed_prefix_and_next_init_regenerates_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_link = os.link
    calls = 0

    def fail_second(source, destination, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_link(source, destination, *args, **kwargs)
        if calls == 2:
            raise OSError("publish fault")
        return result

    monkeypatch.setattr(os, "link", fail_second)
    with pytest.raises(OSError, match="publish fault"):
        _install_template("reviewed-change", "release", tmp_path)
    partial = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert partial

    monkeypatch.setattr(os, "link", original_link)
    _install_template("reviewed-change", "release", tmp_path)
    complete = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert partial.items() <= complete.items()
    assert len(complete) > len(partial)
