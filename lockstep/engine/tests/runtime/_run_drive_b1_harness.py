"""Real command/native setup shared by Task 12 B1 recovery tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from lockstep.runtime.engine import Engine
from lockstep.runtime.service import LockstepCommandService


_NATIVE_FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def _stop_pump(command: LockstepCommandService) -> None:
    command._pump_stop.set()
    command._pump_wakeup.set()
    thread = command._pump_thread
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _install_native_recipes(tmp_path: Path) -> Path:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "native-parent-direct.recipe.yaml").write_bytes(
        (_NATIVE_FIXTURES / "parent_direct.recipe.yaml").read_bytes()
    )
    (recipes / "child_interrupt.recipe.yaml").write_bytes(
        (_NATIVE_FIXTURES / "worker_child_interrupt.recipe.yaml").read_bytes()
    )
    return recipes


@contextmanager
def active_native_command(tmp_path: Path):
    """Yield a real active command with native fixtures and a stopped pump."""

    recipes = _install_native_recipes(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    command = Engine.command(tmp_path / "state", recipes)
    try:
        command._activate_writable_core()
        _stop_pump(command)
        yield command, project
    finally:
        command.close()


@contextmanager
def active_native_manual_park(tmp_path: Path):
    """Yield a real command stopped at a checkpointed manual child interrupt."""

    with active_native_command(tmp_path) as (command, project):
        started = command.start("native-parent-direct", {}, str(project))
        yield command, started["run_id"], project


@contextmanager
def prepared_native_reopen(state_dir: Path, recipes_dir: Path, runtime_context):
    """Reopen real stores/runtime without coupling to legacy watch recovery."""

    assert runtime_context is None
    command = Engine.command(state_dir, recipes_dir)
    reconstruct = command._reconstruct_runtime_execution_context
    try:
        command._reconstruct_runtime_execution_context = lambda: None
        command._prepare_writable_core()
        command._reconstruct_runtime_execution_context = reconstruct
        yield command
    finally:
        command._reconstruct_runtime_execution_context = reconstruct
        command._rollback_writable_core_activation()
        command.close()
