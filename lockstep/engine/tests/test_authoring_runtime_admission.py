"""Runtime start admits exactly one freshly observed canonical recipe DAG."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import lockstep.authoring_publisher as publisher_module
from lockstep import authoring
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import AuthoringPublisher, observe_authoring_project
from lockstep.runtime.engine import LockstepError
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartService
from lockstep.recipe._authority_models import RecipeCandidate
from lockstep.workflow.compiler import canonical_execution_bytes
from tests._authoring_gate import assert_no_durable_runtime_change, tree_image, write_workflow


def _ready(tmp_path: Path, *, state_name: str = "state") -> tuple[Path, Path]:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / state_name).resolve()
    write_workflow(project, "release"); authoring.publish_project_compilation(project, "release", state_dir=state)
    return project, state


def _start(project: Path, state: Path):
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    try: return service.start("release", {}, str(project))
    finally: service.close()


def _stop(captured: list):
    def stop(_self, recipe, plan, _values, *, canonical_input):
        captured.append((recipe, plan, canonical_input)); return {"status": "captured", "run_id": "probe"}
    return stop


def test_public_start_uses_one_locked_canonical_admission(tmp_path, monkeypatch) -> None:
    project, state = _ready(tmp_path); active = False; captured = []; authorizations = []
    original = publisher_module._ExistingAuthoringBoundary.observe
    def observe(self, operation):
        def checked():
            nonlocal active; active = True
            try: return operation()
            finally: active = False
        return original(self, checked)
    original_authorize = RecipeCandidate.authorize
    def authorize(candidate, policy):
        assert active; authorizations.append(candidate); return original_authorize(candidate, policy)
    def stop(*args, **kwargs): return _stop(captured)(*args, **kwargs)
    monkeypatch.setattr(publisher_module._ExistingAuthoringBoundary, "observe", observe)
    monkeypatch.setattr(RecipeCandidate, "authorize", authorize)
    monkeypatch.setattr(AuthorizedStartService, "start", stop)

    assert _start(project, state)["run_id"] == "probe"
    assert len(captured) == 1 and len(authorizations) == 1


def test_public_start_admits_exact_execution_files_and_provenance(tmp_path, monkeypatch) -> None:
    project, state = _ready(tmp_path); captured = []
    monkeypatch.setattr(AuthorizedStartService, "start", _stop(captured))
    _start(project, state)
    _recipe, plan, canonical_input = captured[0]
    recipes = project / ".lockstep/recipes"
    expected = {p.relative_to(recipes).as_posix(): canonical_execution_bytes(p.read_bytes(), logical_path=p.relative_to(recipes).as_posix()) for p in recipes.glob("*.recipe.yaml")}
    observed = {item.path: item.bytes for item in plan.authorized.files}
    proof = plan.compiler_provenance
    assert observed == expected and canonical_input == b"{}"
    assert proof is not None and proof.context == "canonical-match"
    assert {item.relative_path: item.canonical_execution_bytes for item in proof.files} == expected
    assert proof.source_bundle_sha256 == plan.authorized.source_bundle_sha256


@pytest.mark.parametrize("foreign", ("bytes", "symlink", "directory"))
def test_public_start_rejects_noncanonical_before_any_durable_admission(
    tmp_path, monkeypatch, foreign
) -> None:
    project, state = _ready(tmp_path); target = project / ".lockstep/recipes/release.recipe.yaml"
    if foreign == "bytes": target.write_bytes(b"name: foreign\n")
    else:
        target.unlink()
        if foreign == "symlink": target.symlink_to(project / ".lockstep/workflows/release.workflow.yaml")
        else: target.mkdir()
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    reached = []
    monkeypatch.setattr(AuthorizedStartService, "start", lambda *_a, **_k: reached.append(True))
    before = tree_image(state)
    try:
        with pytest.raises((LockstepError, OSError, ValueError)): service.start("release", {}, str(project))
    finally: service.close()
    assert reached == []; assert_no_durable_runtime_change(before, state)


def test_public_start_denies_python_before_import_or_owner_state(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"; project.mkdir(); state = tmp_path / "state"; sentinel = project / "IMPORTED"
    (project / "attacker.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('bad')\ndef run(state): return state\n")
    recipes = project / ".lockstep/recipes"; recipes.mkdir(parents=True)
    (recipes / "release.recipe.yaml").write_text("name: release\ntools:\n  code: {type: python, module: attacker, function: run}\nnodes: {code: {type: python, tool: code}}\nedges: [{from: START, to: code}, {from: code, to: END}]\n")
    monkeypatch.syspath_prepend(str(project)); sys.modules.pop("attacker", None)
    with pytest.raises(LockstepError, match="executable authority denied"): _start(project, state)
    assert not state.exists() and "attacker" not in sys.modules and not sentinel.exists()


@pytest.mark.parametrize("outcome", ("success", "failure"))
def test_observer_discards_optimistic_result_when_boundary_appears(tmp_path, outcome) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve(); calls = []
    def operation():
        calls.append(len(calls));
        if len(calls) == 1:
            journal, _identity = AuthoringJournal.create_for_project(state, project)
            with journal.locked(): pass
            if outcome == "failure": raise LockstepError("optimistic failure")
        return f"result-{len(calls)}"
    assert observe_authoring_project(state, project, operation) == "result-2"
    assert calls == [0, 1]


def test_observer_uses_one_optimistic_plan_while_boundary_absent(tmp_path) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve(); calls = []
    assert observe_authoring_project(state, project, lambda: calls.append(1) or "ok") == "ok"
    assert calls == [1] and not state.exists()


def test_unready_boundary_is_read_only_and_never_repaired(tmp_path) -> None:
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "state").resolve()
    journal, _identity = AuthoringJournal.create_for_project(state, project); before = tree_image(state)
    with pytest.raises(Exception, match="initialization is incomplete"):
        AuthoringPublisher(state).observe(project, lambda: "forbidden")
    assert tree_image(state) == before and not (journal.directory / "transaction.lock").exists()
