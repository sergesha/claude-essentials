"""Template installation shares the bounded authoring writer."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from lockstep.templates import TemplateCollision, install_template

from lockstep import cli
from tests._authoring_gate import tree_image


def _state(project: Path) -> Path:
    return (project.parent / f"{project.name}-state").resolve()


def _cli(project: Path, monkeypatch, capsys):
    monkeypatch.chdir(project); monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(_state(project)))
    code = cli.main(["template", "init", "reviewed-change", "change"])
    output = capsys.readouterr(); return code, output.out, output.err


def _compiled_template_inventory(project: Path):
    descriptors = []
    recipes = project / ".lockstep/recipes"
    closure = (
        recipes / "change.recipe.yaml",
        *sorted((recipes / "generated/children").rglob("*.recipe.yaml")),
    )
    for path in closure:
        document = yaml.safe_load(path.read_text())
        for node in document.get("nodes", {}).values():
            descriptor = node.get("message", {}).get("lockstep_effect")
            if isinstance(descriptor, dict):
                descriptors.append(descriptor)
    effect_kinds = sorted(
        descriptor["kind"]
        for descriptor in descriptors
        if descriptor.get("kind") != "scope"
    )
    requirements = sorted(
        (
            descriptor["kind"],
            descriptor["runner"]["selector"],
            tuple(descriptor["runner"]["required_capabilities"]),
        )
        for descriptor in descriptors
        if descriptor.get("kind") in {"managed", "verify"}
    )
    publications = sorted(
        (item["qualified_handle"], item["destination"])
        for descriptor in descriptors
        if descriptor.get("kind") == "publish"
        for item in descriptor["items"]
    )
    return effect_kinds, requirements, publications


def test_direct_template_install_requires_explicit_external_state(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir(); before = tree_image(project)
    with pytest.raises(TypeError, match="state_dir"):
        install_template("reviewed-change", "change", project)
    assert tree_image(project) == before




@pytest.mark.parametrize(
    ("template", "effect_kinds", "requirements", "publications"),
    (
        (
            "reviewed-change",
            ["accept", "managed", "manual", "manual", "manual", "publish", "verify"],
            [
                (
                    "managed",
                    "codex",
                    (
                        "bounded_result",
                        "credentials",
                        "network",
                        "sandbox",
                        "workspace",
                    ),
                ),
                (
                    "verify",
                    "pinned",
                    ("workspace", "bounded_result", "sandbox"),
                ),
            ],
            [("review.review", ".lockstep/review.md")],
        ),
        (
            "parallel-review",
            ["accept", "accept", "managed", "managed", "publish", "publish"],
            [
                (
                    "managed",
                    "codex",
                    (
                        "bounded_result",
                        "credentials",
                        "network",
                        "sandbox",
                        "workspace",
                    ),
                ),
                (
                    "managed",
                    "codex",
                    (
                        "bounded_result",
                        "credentials",
                        "network",
                        "sandbox",
                        "workspace",
                    ),
                ),
            ],
            [
                (
                    "reviews.architecture.architecture.review",
                    ".lockstep/architecture-review.md",
                ),
                (
                    "reviews.security.security.review",
                    ".lockstep/security-review.md",
                ),
            ],
        ),
    ),
)
def test_installed_templates_compile_exact_runtime_requirements_and_publications(
    tmp_path: Path,
    template: str,
    effect_kinds: list[str],
    requirements: list[tuple[str, str, tuple[str, ...]]],
    publications: list[tuple[str, str]],
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    install_template(template, "change", project, state_dir=_state(project))

    assert _compiled_template_inventory(project) == (
        effect_kinds,
        requirements,
        publications,
    )


def test_project_local_legacy_journal_is_inert_template_data(tmp_path: Path) -> None:
    project = tmp_path / "project"; project.mkdir(); inert = project / ".lockstep/authoring-recovery.json"
    inert.parent.mkdir(); inert.write_bytes(b"project data\n")
    result = install_template("reviewed-change", "change", project, state_dir=_state(project))
    assert inert.read_bytes() == b"project data\n" and result.compile_order == ("change-review", "change")


def test_template_collision_preflight_preserves_nonbasic_artifact(tmp_path: Path) -> None:
    reference = tmp_path / "reference"; reference.mkdir()
    install_template("reviewed-change", "change", reference, state_dir=_state(reference))
    relative = next(p.relative_to(reference) for p in reference.rglob("*.json"))
    project = tmp_path / "project"; project.mkdir(); collision = project / relative
    collision.parent.mkdir(parents=True); collision.write_bytes(b"foreign\n"); before = tree_image(project)
    with pytest.raises(TemplateCollision, match=str(relative)):
        install_template("reviewed-change", "change", project, state_dir=_state(project))
    assert tree_image(project) == before
