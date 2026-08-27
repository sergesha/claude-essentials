"""Private command-owned recovery-driver boundary."""

from __future__ import annotations

from contextlib import contextmanager
from importlib import import_module
from inspect import Parameter, signature
from pathlib import Path
from typing import get_type_hints


@contextmanager
def _prepared_command(tmp_path: Path):
    from lockstep.runtime.engine import Engine

    recipes = tmp_path / "recipes"
    recipes.mkdir()
    command = Engine.command(tmp_path / "state", recipes)
    try:
        command._prepare_writable_core()
        yield command
    finally:
        command._rollback_writable_core_activation()
        command.close()


def test_recovery_driver_has_exact_private_command_composition_surface(
    tmp_path: Path,
) -> None:
    from lockstep.runtime import service as service_module
    from lockstep.runtime import recovery_driver as recovery_driver_module
    from lockstep.runtime.effects.ledger import RunDriveWatch
    from lockstep.runtime.engine import Engine
    from lockstep.runtime.service import LockstepCommandService

    driver_type = getattr(recovery_driver_module, "RecoveryDriver", None)
    assert driver_type is not None
    method = getattr(driver_type, "_drive_run_watch", None)
    assert method is not None
    assert tuple(
        (parameter.name, parameter.kind)
        for parameter in signature(method).parameters.values()
    ) == (
        ("self", Parameter.POSITIONAL_OR_KEYWORD),
        ("watch", Parameter.POSITIONAL_OR_KEYWORD),
    )
    hints = get_type_hints(method)
    assert hints == {"watch": RunDriveWatch, "return": bool}

    with _prepared_command(tmp_path) as command:
        assert type(command._recovery_driver) is driver_type

    assert not hasattr(LockstepCommandService, "_drive_run_watch")
    assert not hasattr(service_module, "RecoveryDriver")
    runtime_package = import_module("lockstep.runtime")
    assert not hasattr(runtime_package, "RecoveryDriver")

    projection = Engine.observe(tmp_path / "state", tmp_path / "recipes")
    try:
        assert not any(
            isinstance(value, driver_type)
            for value in vars(projection).values()
        )
    finally:
        projection.close()
