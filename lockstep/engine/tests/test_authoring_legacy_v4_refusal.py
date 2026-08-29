"""Presence-only refusal for every retained authoring transaction byte shape."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lockstep import authoring
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.recipe._authority_models import RecipeCandidate
from lockstep.runtime.service import LockstepCommandService
from lockstep.runtime.start_service import AuthorizedStartService
from lockstep.templates import install_template
from tests._authoring_gate import replace_marker, tree_image, write_workflow

FIXTURE = Path(__file__).parent / "fixtures/authoring-v4/transaction.json"
PAYLOADS = (pytest.param(FIXTURE.read_bytes(), id="real-v4"), pytest.param(b"{malformed", id="malformed"), pytest.param(b'{"schema":"unknown/v99"}', id="unknown"), pytest.param(b'{"schema":"lockstep.authoring-transaction/v2"}', id="v2"),
    pytest.param(b'{"schema":"lockstep.authoring-transaction/v3"}', id="v3"))

def live_v4_bytes(project: Path) -> bytes:
    document = json.loads(FIXTURE.read_bytes()); old = document["project"]["path"]
    root = project.resolve(); info = root.stat()
    def bind(value):
        if isinstance(value, dict):
            result = {key: bind(item) for key, item in value.items()}
            if "device" in result and "inode" in result: result.update(device=info.st_dev, inode=info.st_ino)
            return result
        if isinstance(value, list): return [bind(item) for item in value]
        if isinstance(value, str) and value.startswith(old): return str(root) + value[len(old):]
        return value
    return json.dumps(bind(document), sort_keys=True, separators=(",", ":")).encode()

def _project(tmp_path: Path):
    project = tmp_path / "project"; project.mkdir(); state = (tmp_path / "owner-state").resolve()
    write_workflow(project, "release"); authoring.publish_project_compilation(project, "release", state_dir=state)
    journal, identity = AuthoringJournal.create_for_project(state, project)
    with journal.locked(): pass
    assert identity.resolved_path == project.resolve()
    return project, state, journal

def _retain(journal: AuthoringJournal, payload: bytes) -> None: journal.journal_path.write_bytes(payload); journal.journal_path.chmod(0o600)

def _guidance(call, project: Path, state: Path, journal: AuthoringJournal) -> None:
    project_before, owner_before = tree_image(project), tree_image(state); raised = pytest.raises(Exception)
    with raised: call()
    message = str(raised.value)
    assert "v4" in message and "pre-simplification" in message
    assert str(project.resolve()) in message and str(state) in message
    assert "Do not delete transaction.json manually" in message
    assert tree_image(project) == project_before and tree_image(state) == owner_before

def test_checked_fixture_rebinds_to_recognized_live_v4(tmp_path) -> None:
    project, state, journal = _project(tmp_path); raw = FIXTURE.read_bytes(); _retain(journal, live_v4_bytes(project))
    with journal.locked(): model = journal.read_recovery_model(expected_project=AuthoringJournal.create_for_project(state, project)[1])
    assert model.project.resolved_path == project.resolve() and len(model.write_set) == 2 and FIXTURE.read_bytes() == raw


@pytest.mark.parametrize("payload", PAYLOADS)
def test_present_bytes_are_refused_without_parsing_or_mutation(tmp_path, monkeypatch, payload) -> None:
    project, state, journal = _project(tmp_path); raw = FIXTURE.read_bytes()
    _retain(journal, live_v4_bytes(project) if payload == raw else payload)
    monkeypatch.setattr(AuthoringJournal, "read_recovery_model", lambda *_a, **_k: pytest.fail("presence refusal parsed transaction bytes"))
    _guidance(lambda: AuthoringPublisher(state).observe(project, lambda: pytest.fail("operation ran")), project, state, journal)
    assert FIXTURE.read_bytes() == raw


def test_live_v4_blocks_all_planning_and_runtime_admission(tmp_path, monkeypatch) -> None:
    import lockstep.templates as templates
    project, state, journal = _project(tmp_path); _retain(journal, live_v4_bytes(project)); replace_marker(project / ".lockstep/workflows/release.workflow.yaml", "initial", "edited")
    reached = []
    def blocked(*_a, **_k): reached.append(True); pytest.fail("planning or admission ran")
    for owner, name in ((authoring, "_plan_project_compilation"), (authoring, "plan_captured_workflow_installation"),
                        (templates, "plan_template_installation"), (RecipeCandidate, "authorize"), (AuthorizedStartService, "start")):
        monkeypatch.setattr(owner, name, blocked)
    monkeypatch.setattr(AuthoringJournal, "read_recovery_model", blocked)
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    calls = (lambda: authoring.publish_project_compilation(project, "release", state_dir=state), lambda: authoring.initialize_minimal(project, "other", state_dir=state),
             lambda: install_template("reviewed-change", "change", project, state_dir=state), lambda: service.start("release", {}, str(project)))
    try:
        for call in calls: _guidance(call, project, state, journal)
    finally: service.close()
    assert reached == []


def test_replacement_project_cannot_escape_retained_binding(tmp_path) -> None:
    project, state, journal = _project(tmp_path); _retain(journal, live_v4_bytes(project)); retired = tmp_path / "retired"; project.rename(retired); project.mkdir(); write_workflow(project, "release")
    before = tree_image(tmp_path); called = []
    with pytest.raises(Exception, match="pre-simplification"): AuthoringPublisher(state).observe(project, lambda: called.append(True))
    assert called == [] and tree_image(tmp_path) == before


@pytest.mark.parametrize("layout", ("state-in-project", "project-in-state"))
def test_state_and_project_namespaces_must_be_disjoint(tmp_path, layout) -> None:
    project = tmp_path / "project"; state = project / "state"
    if layout == "project-in-state": state = tmp_path / "state"; project = state / "project"
    project.mkdir(parents=True); before = tree_image(tmp_path)
    with pytest.raises(ValueError, match="outside the project"): AuthoringPublisher(state.resolve()).observe(project, lambda: "forbidden")
    assert tree_image(tmp_path) == before
