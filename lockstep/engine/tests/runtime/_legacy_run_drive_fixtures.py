"""Shared legacy Gate-B fixtures preserved for the frozen B0 tests."""

from __future__ import annotations

from pathlib import Path

from lockstep.runtime.effects.authority import EffectAuthorityDenied
from lockstep.runtime.service import LockstepCommandService
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import ResolvedCatalog, validate_semantics
from tests.runtime.providers.fakes import (
    FakeEffectAuthority,
    FakeRunner,
    _legacy_command_service,
)


class AutoGrantAuthority(FakeEffectAuthority):
    """Grant legacy test effects while retaining the authority protocol."""

    def __init__(self) -> None:
        super().__init__()
        self.auto_authorize = True

    def resolve(self, intent):
        try:
            return super().resolve(intent)
        except EffectAuthorityDenied:
            if not self.auto_authorize:
                raise
            self.authorize(intent)
            return super().resolve(intent)


def legacy_service(state: Path, recipes: Path) -> LockstepCommandService:
    """Create the frozen legacy command fixture with deterministic providers."""

    return _legacy_command_service(
        state,
        recipes,
        runners={"pinned": FakeRunner()},
        effect_authority=AutoGrantAuthority(),
    )


def compile_recipe(tmp_path: Path, name: str, flow: str):
    """Compile one real workflow recipe into the test recipe directory."""

    source = tmp_path / f"{name}.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        f"name: {name}\n"
        "description: Gate B durable recovery state\n"
        "protect: ['**']\n"
        f"flow:\n{flow}"
    )
    catalog = ResolvedCatalog()
    workflow = parse_workflow(load_workflow(source))
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    recipes = tmp_path / "recipes"
    recipes.mkdir(exist_ok=True)
    for relative_path, content in result.executable_files.items():
        target = recipes / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return recipes, result


def stop_pump(service: LockstepCommandService) -> None:
    """Stop the existing background pump at its deterministic test boundary."""

    service._pump_stop.set()  # noqa: SLF001 - deterministic recovery boundary
    service._pump_wakeup.set()  # noqa: SLF001
    thread = service._pump_thread  # noqa: SLF001
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()
