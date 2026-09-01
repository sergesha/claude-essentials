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


def _template_workflow(bundle_name: str, role: str) -> dict:
    bundle = resources.files("lockstep.templates").joinpath(bundle_name)
    return yaml.safe_load(bundle.joinpath(f"{role}.workflow.yaml").read_text())


def _block_kinds(flow: list[dict]) -> list[str]:
    discriminators = {
        "step",
        "verify",
        "call",
        "accept",
        "parallel",
        "escalate",
    }
    return [next(iter(discriminators & set(block))) for block in flow]


def _assert_semantic_phrases(value: str, phrases: tuple[str, ...]) -> None:
    normalized = " ".join(value.casefold().split())
    assert all(phrase.casefold() in normalized for phrase in phrases)


def _assert_report_cannot_authorize(step: dict) -> None:
    contract = " ".join((step["task"], step["exit"])).casefold()
    assert any(
        phrase in contract
        for phrase in (
            "report text never self-authorizes",
            "report text does not authorize",
            "report text cannot authorize",
            "report is not authorization",
        )
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


def test_reviewed_change_sources_freeze_the_exact_product_inventory() -> None:
    parent = _template_workflow("reviewed-change", "parent")
    child = _template_workflow("reviewed-change", "review")

    assert _block_kinds(parent["flow"]) == [
        "step",
        "step",
        "step",
        "verify",
        "call",
        "accept",
    ]
    plan, tests, implement, verify, call, accept = parent["flow"]
    assert [
        (block["step"], block["writes"], block["retry"])
        for block in (plan, tests, implement)
    ] == [
        ("plan", [".lockstep/plan.md"], {"limit": 2, "exhausted": "escalate"}),
        ("tests", ["tests/"], {"limit": 2, "exhausted": "escalate"}),
        ("implement", ["src/"], {"limit": 2, "exhausted": "escalate"}),
    ]
    assert plan["artifact"] == {
        "handle": "plan",
        "path": ".lockstep/plan.md",
        "markdown": {"sections": ["Goal", "Acceptance Criteria", "Steps"]},
    }
    assert "artifact" not in tests
    assert "artifact" not in implement
    _assert_semantic_phrases(
        plan["task"], ("plan", "goal", "acceptance criteria", "steps")
    )
    _assert_semantic_phrases(plan["exit"], ("plan", "complete"))
    _assert_semantic_phrases(
        tests["task"], ("acceptance tests", "before", "implementation")
    )
    _assert_semantic_phrases(tests["exit"], ("acceptance tests", "frozen"))
    _assert_semantic_phrases(
        implement["task"], ("implementation", "without weakening", "frozen tests")
    )
    _assert_semantic_phrases(implement["exit"], ("implementation", "frozen tests"))
    assert verify == {
        "verify": {
            "id": "tests",
            "command": "pytest -q -p no:cacheprovider",
            "cwd": ".",
            "timeout": 900,
            "retry": {"limit": 2, "exhausted": "escalate"},
        }
    }
    assert call == {
        "call": {
            "id": "review",
            "workflow": "{name}-review",
            "runner": "codex",
            "timeout_minutes": 5,
            "artifacts": {"review": ".lockstep/review.md"},
        }
    }
    assert accept == {
        "accept": {"artifact_from": "review.review", "verdict": "PASS"}
    }

    assert _block_kinds(child["flow"]) == ["step"]
    review = child["flow"][0]
    assert review["step"] == "review"
    assert review["writes"] == ["review.md"]
    assert review["artifact"] == {
        "handle": "review",
        "path": "review.md",
        "markdown": {"sections": ["Findings", "Verdict"]},
    }
    assert "retry" not in review
    _assert_semantic_phrases(
        review["task"],
        (
            "evidence-backed independent review",
            "plan",
            "frozen tests",
            "implementation",
            "pinned verification",
        ),
    )
    _assert_semantic_phrases(
        review["exit"], ("write", "pass", "no blocking finding")
    )
    _assert_report_cannot_authorize(review)


def test_parallel_review_sources_freeze_exact_joined_artifact_inventory() -> None:
    parent = _template_workflow("parallel-review", "parent")
    security_child = _template_workflow("parallel-review", "security-review")
    architecture_child = _template_workflow(
        "parallel-review", "architecture-review"
    )

    assert _block_kinds(parent["flow"]) == ["parallel", "accept", "accept"]
    parallel, security_accept, architecture_accept = parent["flow"]
    parallel = parallel["parallel"]
    assert (parallel["id"], parallel["join"], parallel["timeout_minutes"]) == (
        "reviews",
        "all",
        5,
    )
    calls = {
        branch: blocks[0]["call"] for branch, blocks in parallel["branches"].items()
    }
    assert calls == {
        "security": {
            "id": "security",
            "workflow": "{name}-security-review",
            "runner": "codex",
            "timeout_minutes": 5,
            "artifacts": {"review": ".lockstep/security-review.md"},
        },
        "architecture": {
            "id": "architecture",
            "workflow": "{name}-architecture-review",
            "runner": "codex",
            "timeout_minutes": 5,
            "artifacts": {"review": ".lockstep/architecture-review.md"},
        },
    }
    assert all("retry" not in call for call in calls.values())
    assert [security_accept, architecture_accept] == [
        {
            "accept": {
                "artifact_from": "reviews.security.security.review",
                "verdict": "PASS",
            }
        },
        {
            "accept": {
                "artifact_from": "reviews.architecture.architecture.review",
                "verdict": "PASS",
            }
        },
    ]

    assert _block_kinds(security_child["flow"]) == ["step"]
    assert _block_kinds(architecture_child["flow"]) == ["step"]
    for child, path in (
        (security_child, "security-review.md"),
        (architecture_child, "architecture-review.md"),
    ):
        step = child["flow"][0]
        assert step["writes"] == [path]
        assert step["artifact"] == {
            "handle": "review",
            "path": path,
            "markdown": {"sections": ["Findings", "Verdict"]},
        }
        assert "retry" not in step

    security = security_child["flow"][0]
    _assert_semantic_phrases(
        security["task"],
        (
            "reachable boundaries",
            "frozen threat model",
            "boundary",
            "pre-existing authority",
            "achieved authority",
            "delta",
        ),
    )
    _assert_semantic_phrases(security["exit"], ("findings", "verdict"))
    _assert_report_cannot_authorize(security)

    architecture = architecture_child["flow"][0]
    _assert_semantic_phrases(
        architecture["task"],
        (
            "responsibility",
            "dependency direction",
            "cohesion",
            "public-contract preservation",
        ),
    )
    _assert_semantic_phrases(architecture["exit"], ("findings", "verdict"))
    _assert_report_cannot_authorize(architecture)


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
