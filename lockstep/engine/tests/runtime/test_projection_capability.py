from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep import cli
from lockstep.mcp import server
from lockstep.runtime.engine import Engine
from lockstep.runtime.service import LockstepService


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


def _stop_pump(service: LockstepService) -> None:
    service._pump_stop.set()  # noqa: SLF001 - deterministic real crash boundary
    service._pump_wakeup.set()  # noqa: SLF001
    thread = service._pump_thread  # noqa: SLF001
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _seed_recoverable_run(project: Path, state: Path, recipes: Path) -> str:
    """Leave a real admitted start watch before its first native checkpoint."""

    service = LockstepService(state, recipes)
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
        watches = service.effects.list_dispatch_watches(limit=2)
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
        active = getattr(server, "_engine", None)
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
