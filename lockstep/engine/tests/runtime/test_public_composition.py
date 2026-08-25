"""Task 12R0 Gate A-schema: independently missing public contracts on b794."""

from __future__ import annotations

import argparse
import importlib

import pytest

from lockstep import cli
from lockstep.runtime.engine import Engine


def _contract_module(module_name: str, contract: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        pytest.fail(
            f"{contract} is absent: module {module_name} is missing",
            pytrace=False,
        )


@pytest.mark.parametrize(
    "factory_name",
    ["observe", "command"],
)
def test_engine_exposes_explicit_capability_factory(
    factory_name: str,
) -> None:
    factory = getattr(Engine, factory_name, None)
    assert callable(factory), f"Engine.{factory_name} is absent"


def test_runtime_projection_public_module_exists() -> None:
    _contract_module(
        "lockstep.runtime.projection",
        "runtime projection public module surface",
    )


def test_command_service_public_type_exists() -> None:
    module = _contract_module(
        "lockstep.runtime.service",
        "LockstepCommandService public command capability",
    )
    command_type = getattr(module, "LockstepCommandService", None)
    assert command_type is not None, "LockstepCommandService is absent"


def test_released_runner_composition_public_module_exists() -> None:
    _contract_module(
        "lockstep.runtime.providers.composition",
        "released runner composition public module surface",
    )


def test_owner_policy_public_module_exists() -> None:
    _contract_module(
        "lockstep.runtime.effects.owner_policy",
        "owner-policy public module surface",
    )


def test_owner_cli_public_verb_exists() -> None:
    parser = cli._build_parser()  # noqa: SLF001 - public grammar contract
    root_subparsers = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    )
    assert "owner" in root_subparsers.choices, "lockstep owner verb is absent"
