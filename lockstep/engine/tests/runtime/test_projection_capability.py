from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep import cli
from lockstep.mcp import server
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.service import LockstepCommandService

FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def _context(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(
            meta={"x-codex-turn-metadata": {"workspaces": {str(project): {}}}}
        )
    )


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(tmp_path / "owner-state"))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(recipes))
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    child = (FIXTURES / "worker_child_interrupt.recipe.yaml").read_text()
    (recipes / "child_interrupt.recipe.yaml").write_text(
        child.replace("name: native-child-interrupt", "name: child_interrupt")
    )
    server._reset_engine()
    return project


def _stop_pump(service: LockstepCommandService) -> None:
    service._activate_writable_core()  # noqa: SLF001 - crash-boundary fixture
    service._pump_stop.set()  # noqa: SLF001 - deterministic real crash boundary
    service._pump_wakeup.set()  # noqa: SLF001
    thread = service._pump_thread  # noqa: SLF001
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _seed_recoverable_run(project: Path, state: Path, recipes: Path) -> str:
    """Leave a real admitted start watch before its first native checkpoint."""

    service = LockstepCommandService(state, recipes)
    _stop_pump(service)
    real_start = service.runtime.ensure_started

    def crash_before_first_checkpoint(_run_id, _values):
        raise RuntimeError("crash before first checkpoint")

    service.runtime.ensure_started = crash_before_first_checkpoint
    try:
        with pytest.raises(RuntimeError, match="crash before first checkpoint"):
            service.start("native-parent-direct", {}, str(project))
        bindings = service.catalog.list(str(project.resolve()))
        assert len(bindings) == 1
        high_water = service.effects.max_run_drive_admission_seq()
        assert high_water is not None
        watches = service.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=2,
        )
        assert [watch.public_run_id for watch in watches] == [
            bindings[0].public_run_id
        ]
        return bindings[0].public_run_id
    finally:
        service.runtime.ensure_started = real_start
        service.close()


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    return tuple(
        (str(path.relative_to(root)), path.stat().st_mode & 0o777, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.name.endswith(("-wal", "-shm", "-journal"))
    )


def _changed_paths(
    before: tuple[tuple[str, int, bytes], ...],
    after: tuple[tuple[str, int, bytes], ...],
) -> tuple[str, ...]:
    old = {path: (mode, content) for path, mode, content in before}
    new = {path: (mode, content) for path, mode, content in after}
    return tuple(
        path for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)
    )


def test_public_engine_has_no_implicit_active_constructor(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Engine(tmp_path / "owner-state", tmp_path / "recipes")


def test_projection_rejects_insecure_existing_owner_root_without_database(
    tmp_path: Path,
) -> None:
    state = tmp_path / "owner-state"
    state.mkdir(mode=0o755)
    recipes = tmp_path / "recipes"
    recipes.mkdir()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).list_runs(str(tmp_path))


def test_projection_rejects_symlinked_owner_root_before_canonicalization(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual-owner-state"
    actual.mkdir(mode=0o700)
    state = tmp_path / "owner-state"
    state.symlink_to(actual, target_is_directory=True)
    recipes = tmp_path / "recipes"
    recipes.mkdir()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).list_runs(str(tmp_path))


def test_projection_status_does_not_parse_an_unrelated_thread_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "INSERT INTO effects ("
            "effect_id, thread_id, checkpoint_ns, checkpoint_id, task_id, "
            "interrupt_id, descriptor_digest, effect_kind, deadline_at, phase, "
            "lease_epoch, created_at, updated_at, revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "unrelated-effect",
                "unrelated-thread",
                "",
                "checkpoint",
                "task",
                "interrupt",
                "0" * 64,
                "manual",
                "not-an-iso-timestamp",
                "pending",
                0,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    assert Engine.observe(state, recipes).status(run_id, str(project))["run_id"] == run_id


def test_projection_events_fail_closed_on_relevant_malformed_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    command = LockstepCommandService(state, recipes)
    try:
        run_id = command.start("native-parent-direct", {}, str(project))["run_id"]
    finally:
        command.close()
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "UPDATE effects SET updated_at = ? WHERE thread_id = "
            "(SELECT thread_id FROM runs WHERE public_run_id = ?)",
            ("not-an-iso-timestamp", run_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).events(run_id, str(project))


def test_projection_status_fails_closed_on_relevant_unknown_effect_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    command = LockstepCommandService(state, recipes)
    try:
        run_id = command.start("native-parent-direct", {}, str(project))["run_id"]
    finally:
        command.close()
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        cursor = connection.execute(
            "UPDATE effects SET phase = ? WHERE thread_id = "
            "(SELECT thread_id FROM runs WHERE public_run_id = ?)",
            ("unknown", run_id),
        )
        assert cursor.rowcount == 1
        connection.commit()
    finally:
        connection.close()

    projection = Engine.observe(state, recipes)
    try:
        with pytest.raises(
            LockstepError,
            match="trusted native state failed read-only verification",
        ):
            projection.status(run_id, str(project))
    finally:
        projection.close()


def test_projection_rejects_catalog_recipe_digest_not_backed_by_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    connection = sqlite3.connect(state / "runtime.sqlite")
    try:
        connection.execute(
            "UPDATE runs SET recipe_digest = ? WHERE public_run_id = ?",
            ("f" * 64, run_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).status(run_id, str(project))


@pytest.mark.parametrize("poison", ["symlink", "shared-mode", "oversize"])
def test_projection_rejects_poisoned_session_binding(
    poison: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    command = LockstepCommandService(state, recipes)
    try:
        run_id = command.start("native-parent-direct", {}, str(project))["run_id"]
        _stop_pump(command)
    finally:
        command.close()
    bindings = state / "bindings"
    bindings.mkdir(mode=0o700, exist_ok=True)
    binding = bindings / f"{run_id}.json"
    if poison == "symlink":
        outside = tmp_path / "outside-binding.json"
        outside.write_text(
            '{"session_id":"attacker","last_seen":"9999-01-01T00:00:00+00:00"}',
            encoding="utf-8",
        )
        binding.symlink_to(outside)
    elif poison == "shared-mode":
        binding.write_text(
            '{"session_id":"attacker","last_seen":"9999-01-01T00:00:00+00:00"}',
            encoding="utf-8",
        )
        binding.chmod(0o604)
    else:
        binding.write_bytes(b"{" + b" " * (64 * 1024))
        binding.chmod(0o600)

    with pytest.raises(
        LockstepError,
        match="trusted native state failed read-only verification",
    ):
        Engine.observe(state, recipes).status(run_id, str(project))


def test_projection_ignores_poisoned_owner_runtime_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    expected = Engine.observe(state, recipes).status(run_id, str(project))
    runtime_owner = state / "runtime-owner"
    runtime_owner.mkdir(mode=0o700)
    outside = tmp_path / "poisoned-snapshot.json"
    outside.write_text("not trusted owner state", encoding="utf-8")
    (runtime_owner / "snapshot.json").symlink_to(outside)

    assert Engine.observe(state, recipes).status(run_id, str(project)) == expected


def test_projection_status_preserves_owner_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    before = _tree_snapshot(state)

    result = Engine.observe(state, recipes).status(run_id, str(project))

    assert result["run_id"] == run_id
    assert _tree_snapshot(state) == before


@pytest.mark.parametrize("operation", ["status", "wait", "history", "events"])
def test_cold_cli_observations_do_not_drive_unrelated_run(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read verb selects projection before any active runtime is constructed."""

    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    before = _tree_snapshot(state)
    argv = ["scenario", operation, run_id]
    if operation == "wait":
        argv.extend(["--timeout", "1"])

    assert cli.main(argv) == 0
    after = _tree_snapshot(state)
    assert after == before, (
        f"cold CLI {operation} mutated unrelated recoverable state: "
        f"{_changed_paths(before, after)}"
    )


@pytest.mark.parametrize(
    ("operation", "invoke"),
    [
        (
            "status",
            lambda project, run_id: server.scenario_status(
                run_id, ctx=_context(project)
            ),
        ),
        (
            "wait",
            lambda project, run_id: server.scenario_wait(
                run_id, timeout_seconds=1, ctx=_context(project)
            ),
        ),
        (
            "history",
            lambda project, run_id: server.scenario_history(
                run_id, ctx=_context(project)
            ),
        ),
        (
            "events",
            lambda project, run_id: server.scenario_events(
                run_id, ctx=_context(project)
            ),
        ),
        ("list", lambda project, _run_id: server.list_runs(ctx=_context(project))),
        (
            "trace",
            lambda project, run_id: server.run_trace(run_id, ctx=_context(project)),
        ),
    ],
)
def test_cold_mcp_observations_do_not_construct_driver(
    operation: str,
    invoke,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every MCP read uses the projection handle and leaves command state absent."""

    project = _configure(tmp_path, monkeypatch)
    state = tmp_path / "owner-state"
    recipes = project / ".lockstep" / "recipes"
    run_id = _seed_recoverable_run(project, state, recipes)
    server._reset_engine()
    before = _tree_snapshot(state)
    try:
        invoke(project, run_id)
        after = _tree_snapshot(state)
        active = getattr(server, "_command", None)
        assert {
            "operation": operation,
            "facts_unchanged": after == before,
            "changed_paths": _changed_paths(before, after),
            "active_command_singleton": active is not None,
            "active_command_parts": ()
            if active is None
            else tuple(
                sorted(
                    name
                    for name in (
                        "manual",
                        "coordinator",
                        "authority",
                        "runners",
                        "_pump_thread",
                        "_pump_failure",
                    )
                    if hasattr(active, name)
                )
            ),
        } == {
            "operation": operation,
            "facts_unchanged": True,
            "changed_paths": (),
            "active_command_singleton": False,
            "active_command_parts": (),
        }
    finally:
        server._reset_engine()
