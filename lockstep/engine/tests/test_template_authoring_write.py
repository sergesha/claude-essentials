"""Template installation shares the bounded authoring writer."""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest
import yaml

import lockstep.authoring_publisher as publisher_module
from lockstep import cli
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.templates import TemplateCollision, install_template
from tests._authoring_gate import tree_image
from tests.test_authoring_legacy_v4_refusal import (
    _create_test_namespace,
    _retain,
    live_v4_bytes,
)


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
    state = inspect.signature(install_template).parameters["state_dir"]
    assert state.kind is inspect.Parameter.KEYWORD_ONLY and state.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="state_dir"):
        install_template("reviewed-change", "change", project)
    assert tree_image(project) == before


def test_cli_template_init_routes_one_destination_only_plan_through_ready_publisher(
    tmp_path, monkeypatch, capsys
) -> None:
    import lockstep.templates as templates
    project = tmp_path / "project"; project.mkdir(); events = []
    original_plan, original_publish = templates.plan_template_installation, AuthoringPublisher.publish
    monkeypatch.setattr(AuthoringPublisher, "require_ready", lambda _s, root: events.append(("ready", root)), raising=False)
    def plan(*args, **kwargs):
        value = original_plan(*args, **kwargs); events.append(("plan", value.plan)); return value
    def publish(self, value): events.append(("publish", value)); return original_publish(self, value)
    monkeypatch.setattr(templates, "plan_template_installation", plan)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)

    assert _cli(project, monkeypatch, capsys) == (0, "initialized change\n", "")
    assert [item[0] for item in events] == ["ready", "plan", "publish"]
    plan = events[1][1]
    assert plan.sources == () and plan.dependency_edges == (("change-review", ()), ("change", ("change-review",)))
    assert all(item.before is None for item in plan.targets)
    assert {item.path for item in plan.targets} == {p.resolve() for p in project.rglob("*") if p.is_file()}


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


def test_owner_transaction_refusal_precedes_template_planning(tmp_path, monkeypatch) -> None:
    import lockstep.templates as templates
    project = tmp_path / "project"; project.mkdir(); state = _state(project)
    namespace, _identity = _create_test_namespace(state, project)
    _retain(namespace, live_v4_bytes(project))
    before_project, before_state = tree_image(project), tree_image(state)
    planned = []
    monkeypatch.setattr(templates, "plan_template_installation", lambda *_a, **_k: planned.append(True))
    with pytest.raises(Exception, match="pre-simplification"):
        install_template("reviewed-change", "change", project, state_dir=state)
    assert planned == [] and tree_image(project) == before_project and tree_image(state) == before_state


def test_partial_template_write_is_not_rolled_back_and_regenerates_remainder(
    tmp_path, monkeypatch
) -> None:
    project = tmp_path / "project"; project.mkdir(); state = _state(project); published = []
    original_link = os.link
    def link_then_die(*args, **kwargs):
        original_link(*args, **kwargs); published.append(os.fsdecode(args[1])); raise BaseException("cut")
    def publish(_self, plan): return publisher_module._publish_per_file(plan)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)
    monkeypatch.setattr(os, "link", link_then_die)
    with pytest.raises(BaseException, match="cut"):
        install_template("reviewed-change", "change", project, state_dir=state)
    assert len(published) == 1 and any(p.is_file() for p in project.rglob("*"))
    with monkeypatch.context() as clean:
        clean.setattr(os, "link", original_link)
        installed = install_template("reviewed-change", "change", project, state_dir=state)
    assert all(path.is_file() for path in (*installed.sources, *installed.recipes))
