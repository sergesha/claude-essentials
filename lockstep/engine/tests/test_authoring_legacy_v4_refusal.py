"""Every state-aware surface fails closed on retained transaction evidence."""
from __future__ import annotations

from pathlib import Path

import pytest

from lockstep import authoring
from lockstep.authoring_journal import AuthoringJournal
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime.service import LockstepCommandService
from lockstep.templates import install_template
from tests._authoring_gate import replace_marker, tree_image, write_workflow


FIXTURE = Path(__file__).parent / "fixtures/authoring-v4/transaction.json"
PAYLOADS = (
    pytest.param(FIXTURE.read_bytes(), id="real-v4"),
    pytest.param(b"{malformed", id="malformed"),
    pytest.param(b'{"schema":"unknown/v99"}', id="unknown"),
    pytest.param(b'{"schema":"lockstep.authoring-transaction/v2"}', id="v2"),
    pytest.param(b'{"schema":"lockstep.authoring-transaction/v3"}', id="v3"),
)


def _project(tmp_path: Path) -> tuple[Path, Path, AuthoringJournal]:
    project = tmp_path / "project"; project.mkdir()
    state = (tmp_path / "owner-state").resolve(); write_workflow(project, "release")
    authoring.publish_project_compilation(project, "release", state_dir=state)
    journal, identity = AuthoringJournal.create_for_project(state, project)
    assert identity.resolved_path == project.resolve()
    with journal.locked(): pass
    assert journal.directory == state / "authoring" / journal.directory.name
    return project, state, journal


def _retain(journal: AuthoringJournal, payload: bytes) -> None:
    journal.journal_path.write_bytes(payload); journal.journal_path.chmod(0o600)


def _assert_guidance(call, project: Path, state: Path, journal: AuthoringJournal) -> None:
    before = journal.journal_path.read_bytes()
    with pytest.raises(Exception) as raised: call()
    message = str(raised.value)
    assert "v4" in message and "pre-simplification" in message
    assert str(project.resolve()) in message and str(state) in message
    assert "Do not delete transaction.json manually" in message
    assert journal.journal_path.read_bytes() == before


@pytest.mark.parametrize("payload", PAYLOADS)
def test_present_transaction_bytes_share_one_read_only_refusal(
    tmp_path: Path, payload: bytes
) -> None:
    project, state, journal = _project(tmp_path); _retain(journal, payload)
    project_before, owner_before = tree_image(project), tree_image(state)
    _assert_guidance(lambda: AuthoringPublisher(state).observe(project, lambda: "ran"), project, state, journal)
    assert tree_image(project) == project_before and tree_image(state) == owner_before


def test_v4_blocks_writers_and_real_runtime_before_planning_or_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, journal = _project(tmp_path); _retain(journal, FIXTURE.read_bytes())
    replace_marker(project / ".lockstep/workflows/release.workflow.yaml", "initial", "edited")
    service = LockstepCommandService(state, project / ".lockstep/recipes")
    calls = (
        lambda: authoring.publish_project_compilation(project, "release", state_dir=state),
        lambda: authoring.initialize_minimal(project, "other", state_dir=state),
        lambda: install_template("reviewed-change", "change", project, state_dir=state),
        lambda: service.start("release", {}, str(project)),
    )
    try:
        for call in calls: _assert_guidance(call, project, state, journal)
    finally: service.close()


def test_raw_render_and_estimate_remain_state_free_with_v4_evidence(tmp_path: Path) -> None:
    project, _state, journal = _project(tmp_path); _retain(journal, FIXTURE.read_bytes())
    assert "workflow_version" in authoring.render_recipe(project, "release", "workflow")
    assert authoring.estimate_recipe(project, "release")["schema"] == "lockstep.structural-estimate/v1"
