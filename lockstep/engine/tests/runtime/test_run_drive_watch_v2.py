"""Public recovery behavior across a full page of parked runs."""

from __future__ import annotations

from pathlib import Path

import pytest
from lockstep.runtime import sessions
from lockstep.runtime.engine import Engine

from tests.runtime._legacy_run_drive_fixtures import (
    AutoGrantAuthority,
    compile_recipe,
    stop_pump,
)
from tests.runtime.providers.fakes import FakeRunner, _legacy_command_service


def _status(state: Path, recipes: Path, project: Path, run_id: str) -> dict:
    observer = Engine.observe(state, recipes)
    try:
        return observer.status(run_id, str(project))
    finally:
        observer.close()


def test_recovery_reaches_later_run_past_revoked_and_full_parked_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipes, managed = compile_recipe(
        tmp_path,
        "blocked-managed",
        "  - verify:\n"
        "      id: tests\n"
        "      command: pytest -q\n"
        "      timeout: 60\n",
    )
    _recipes, parked = compile_recipe(
        tmp_path,
        "worker-park",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n",
    )
    _recipes, late = compile_recipe(
        tmp_path,
        "late-decision",
        "  - step: edit\n"
        "    task: Edit the project\n"
        "    exit: Editing is complete\n"
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n",
    )
    state = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    authority = AutoGrantAuthority()
    runner = FakeRunner()
    command = _legacy_command_service(
        state,
        recipes,
        runners={"pinned": runner},
        effect_authority=authority,
    )
    stop_pump(command)
    try:
        blocked_id = command.start(
            "blocked-managed",
            {},
            str(project),
            compiler_provenance=managed.compiler_provenance,
        )["run_id"]
        authority.revoke(authority.resolve_intents[-1].intent_digest)
        blocked_before = _status(state, recipes, project, blocked_id)
        provider_calls_before = (
            len(runner.prepare_calls),
            len(runner.ensure_started_calls),
            runner.spawn_count,
        )

        for _index in range(129):
            command.start(
                "worker-park",
                {},
                str(project),
                compiler_provenance=parked.compiler_provenance,
            )

        late_id = command.start(
            "late-decision",
            {},
            str(project),
            compiler_provenance=late.compiler_provenance,
        )["run_id"]
        sessions.touch(state, late_id, "worker", 30)

        def crash_after_durable_delivery(*_args, **_kwargs):
            raise RuntimeError("simulated process death")

        monkeypatch.setattr(command, "_drive_engine_owned", crash_after_durable_delivery)
        with pytest.raises(RuntimeError, match="simulated process death"):
            command.scenario_done(
                late_id,
                "edit",
                {},
                session_id="worker",
                project=str(project),
            )
    finally:
        command.close()

    restarted = _legacy_command_service(
        state,
        recipes,
        runners={"pinned": runner},
        effect_authority=authority,
    )
    try:
        restarted.scenario_recover(str(project), limit=128)
        late_status = _status(state, recipes, project, late_id)
        assert late_status["status"] == "escalated"
        assert _status(state, recipes, project, blocked_id) == blocked_before
        assert (
            len(runner.prepare_calls),
            len(runner.ensure_started_calls),
            runner.spawn_count,
        ) == provider_calls_before
    finally:
        restarted.close()
