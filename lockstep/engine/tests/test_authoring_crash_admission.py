"""Canonical-iff runtime admission after every bounded-writer crash cut."""
from __future__ import annotations

import json, multiprocessing, os, stat
from dataclasses import dataclass
from pathlib import Path

import pytest

import lockstep.authoring_publisher as publisher
from lockstep import authoring
from lockstep.authoring_bundle import ProjectCompilationBundle, plan_project_compilation
from lockstep.authoring_installation import CapturedWorkflowSource, plan_captured_workflow_installation
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartService
from lockstep.workflow.compiler import canonical_execution_bytes
from lockstep.template_installation import plan_template_installation
from lockstep.templates import install_template
from tests._authoring_gate import assert_no_durable_runtime_change, replace_marker, tree_image, write_workflow


CUT_EXIT = 86


@dataclass(frozen=True)
class Scenario:
    project: Path; state: Path; root: str; bundle: ProjectCompilationBundle
    source: Path | None = None; old_source: bytes | None = None

    @property
    def targets(self): return tuple(item.resolved_path for item in self.bundle.after_images)


def _scenario(root: Path, surface: str, present: bool) -> Scenario:
    root.mkdir(parents=True, exist_ok=True); project = root / "project"; project.mkdir(); state = (root / "state").resolve()
    if surface == "compile":
        source = write_workflow(project, "release"); old = source.read_bytes()
        if present: authoring.publish_project_compilation(project, "release", state_dir=state); replace_marker(source, "initial", "new")
        return Scenario(project, state, "release", plan_project_compilation(authoring.project_paths(project, "release")), source, old)
    if surface == "minimal":
        source = authoring._minimal_workflow_source("release")
        plan = plan_captured_workflow_installation(project, (CapturedWorkflowSource("release", source),), root_role="release")
        return Scenario(project, state, "release", plan.bundle)
    import lockstep.templates as templates
    manifest = templates._manifest("reviewed-change")
    sources = templates._captured_role_sources("reviewed-change", "change", manifest)
    plan = plan_template_installation(project, sources, root_role="change")
    return Scenario(project, state, "change", plan.bundle)


def _die() -> None: os._exit(CUT_EXIT)


def _nth(original, ordinal: int):
    calls = 0
    def wrapped(*args, **kwargs):
        nonlocal calls; result = original(*args, **kwargs)
        if calls == ordinal: _die()
        calls += 1; return result
    return wrapped


def _install_cut(phase: str, ordinal: int) -> None:
    if phase == "after-temp-creation": publisher._create_temporary = _nth(publisher._create_temporary, ordinal); return
    if phase == "after-temp-fsync": publisher._write_temporary = _nth(publisher._write_temporary, ordinal); return
    if phase == "after-final-validation": publisher.validate_destination_before_at = _nth(publisher.validate_destination_before_at, ordinal); return
    if phase == "after-mutation": publisher._publish_owned_temporary = _nth(publisher._publish_owned_temporary, ordinal); return
    if phase == "after-target-fsync": publisher._fsync_regular_at = _nth(publisher._fsync_regular_at, ordinal); return
    if phase == "after-parent-fsync":
        current = -1; original_target, original_fsync = publisher._fsync_regular_at, os.fsync
        def target(*args, **kwargs):
            nonlocal current; original_target(*args, **kwargs); current += 1
        def fsync(descriptor):
            result = original_fsync(descriptor)
            if current == ordinal and stat.S_ISDIR(os.fstat(descriptor).st_mode): _die()
            return result
        publisher._fsync_regular_at, os.fsync = target, fsync; return
    raise AssertionError(phase)


def _public_write(scenario: Scenario, surface: str) -> None:
    if surface == "compile": authoring.publish_project_compilation(scenario.project, "release", state_dir=scenario.state)
    elif surface == "minimal": authoring.initialize_minimal(scenario.project, "release", state_dir=scenario.state)
    else: install_template("reviewed-change", "change", scenario.project, state_dir=scenario.state)


def _crash_child(scenario: Scenario, surface: str, phase: str, ordinal: int, force_route: bool) -> None:
    if force_route: AuthoringPublisher.publish = lambda _self, bundle: publisher._publish_per_file(bundle)
    _install_cut(phase, ordinal)
    _public_write(scenario, surface)
    os._exit(0)


def _run_cut(scenario: Scenario, surface: str, phase: str, ordinal: int, *, force_route: bool = False) -> int:
    process = multiprocessing.get_context("fork").Process(target=_crash_child, args=(scenario, surface, phase, ordinal, force_route))
    process.start(); process.join(20)
    if process.is_alive(): process.kill(); process.join(); pytest.fail("public writer child hung")
    assert process.exitcode is not None
    return process.exitcode


def _semantics(scenario: Scenario) -> tuple[int, bool]:
    changed = 0
    for target, after in zip(scenario.targets, scenario.bundle.after_images, strict=True):
        if target.is_file() and not target.is_symlink() and target.read_bytes() == after.content and stat.S_IMODE(target.stat().st_mode) == after.mode: changed += 1
    old_complete = all(item.content is not None for item in scenario.bundle.before_images) and changed == 0
    return changed, old_complete


def _authorized_dag(surface: str, authorized) -> None:
    root = "change.recipe.yaml" if surface == "template" else "release.recipe.yaml"
    files = tuple(item.path for item in authorized.files)
    assert authorized.root == root and authorized.dependency_dag.root == root
    assert authorized.dependency_dag.files == files
    edges = set()
    for item in authorized.files:
        document = json.loads(item.bytes)
        for node in document["nodes"].values():
            if node.get("type") == "subgraph": edges.add((item.path, node["graph"]))
    if surface != "template": assert files == (root,) and edges == set()
    else:
        generated = next(path for path in files if path.startswith("generated/children/"))
        assert files == ("change-review.recipe.yaml", "change.recipe.yaml", generated)
        assert edges == {("change.recipe.yaml", "change-review.recipe.yaml"), ("change.recipe.yaml", generated)}


def _runtime_oracle(scenario: Scenario, monkeypatch, accept: bool, surface: str) -> None:
    captured = []
    def stop(_self, recipe, plan, _values, *, canonical_input):
        captured.append((recipe, plan, canonical_input)); return {"status": "captured", "run_id": "probe"}
    monkeypatch.setattr(AuthorizedStartService, "start", stop)
    service = LockstepCommandService(scenario.state, scenario.project / ".lockstep/recipes")
    before = tree_image(scenario.state)
    try:
        if not accept:
            with pytest.raises(Exception): service.start(scenario.root, {}, str(scenario.project))
            assert captured == []; assert_no_durable_runtime_change(before, scenario.state); return
        assert service.start(scenario.root, {}, str(scenario.project))["run_id"] == "probe"
    finally: service.close()
    assert len(captured) == 1
    _recipe, plan, canonical_input = captured[0]; recipes = scenario.project / ".lockstep/recipes"
    expected = {path.relative_to(recipes).as_posix(): canonical_execution_bytes(path.read_bytes(), logical_path=path.relative_to(recipes).as_posix()) for path in recipes.rglob("*.recipe.yaml")}
    assert {item.path: item.bytes for item in plan.authorized.files} == expected
    _authorized_dag(surface, plan.authorized)
    proof = plan.compiler_provenance; assert proof is not None and proof.context == "canonical-match"
    assert {item.relative_path: item.canonical_execution_bytes for item in proof.files} == expected
    assert proof.source_bundle_sha256 == plan.authorized.source_bundle_sha256 and canonical_input == b"{}"


CASES = (("compile", False), ("compile", True), ("minimal", False), ("template", False))
PHASES = ("after-temp-creation", "after-temp-fsync", "after-final-validation", "after-mutation", "after-target-fsync", "after-parent-fsync")


@pytest.mark.parametrize(("surface", "present"), CASES)
@pytest.mark.parametrize("phase", PHASES)
def test_every_public_writer_cut_has_canonical_iff_runtime_admission(
    tmp_path, monkeypatch, surface, present, phase
) -> None:
    probe = _scenario(tmp_path / "probe", surface, present)
    for ordinal in range(len(probe.targets)):
        cell = tmp_path / f"{surface}-{present}-{phase}-{ordinal}"; cell.mkdir()
        scenario = _scenario(cell, surface, present)
        assert _run_cut(scenario, surface, phase, ordinal) == CUT_EXIT
        cut_project = tree_image(scenario.project)
        changed, old_complete = _semantics(scenario)
        new_complete = changed == len(scenario.targets)
        assert not (old_complete and scenario.source is not None and scenario.source.read_bytes() == scenario.old_source)
        with monkeypatch.context() as runtime: _runtime_oracle(scenario, runtime, new_complete, surface)
        assert tree_image(scenario.project) == cut_project


def test_subprocess_cut_skips_finally_cleanup_and_preserves_mixed_stale_tree(tmp_path, monkeypatch) -> None:
    absent = _scenario(tmp_path / "absent", "minimal", False)
    assert _run_cut(absent, "minimal", "after-temp-creation", 0, force_route=True) == CUT_EXIT
    assert tuple(absent.project.rglob(".lockstep-authoring-*.tmp"))
    stale = _scenario(tmp_path / "stale", "compile", True)
    assert _run_cut(stale, "compile", "after-mutation", 0, force_route=True) == CUT_EXIT
    snapshot = tree_image(stale.project); assert stale.source is not None and b"new" in stale.source.read_bytes()
    changed, _old = _semantics(stale); assert 0 < changed < len(stale.targets)
    _runtime_oracle(stale, monkeypatch, False, "compile"); assert tree_image(stale.project) == snapshot


@pytest.mark.parametrize("surface", ("compile", "minimal", "template"))
def test_complete_last_target_cut_admits_the_exact_surface_dag(tmp_path, monkeypatch, surface) -> None:
    scenario = _scenario(tmp_path, surface, False); last = len(scenario.targets) - 1
    assert _run_cut(scenario, surface, "after-parent-fsync", last, force_route=True) == CUT_EXIT
    assert _semantics(scenario) == (len(scenario.targets), False)
    _runtime_oracle(scenario, monkeypatch, True, surface)


@pytest.mark.parametrize("surface", ("compile", "minimal", "template"))
def test_public_writer_surfaces_route_to_the_bounded_per_file_writer(tmp_path, monkeypatch, surface) -> None:
    scenario = _scenario(tmp_path, surface, False); seen = []; original = publisher._publish_per_file
    monkeypatch.setattr(publisher, "_publish_per_file", lambda bundle: seen.append(bundle) or original(bundle))
    _public_write(scenario, surface)
    assert seen == [scenario.bundle]


def test_final_verification_rejects_foreign_earlier_target_without_rollback(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, "minimal", False); first, last = scenario.targets[0], scenario.targets[-1]
    original = publisher.capture_after_identity_at; tampered = b"foreign\n"
    def capture(parent, after):
        value = original(parent, after)
        if after.resolved_path == last: first.write_bytes(tampered)
        return value
    monkeypatch.setattr(publisher, "capture_after_identity_at", capture)
    with pytest.raises(Exception): publisher._publish_per_file(scenario.bundle)
    assert first.read_bytes() == tampered
