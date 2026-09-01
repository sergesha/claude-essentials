"""Task 9 native-child public restart contracts (real yamlgraph only)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.engine import Engine
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.service import LockstepError, LockstepCommandService
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
    parse_scope_result,
)
from lockstep.runtime.effects.authority import EffectAuthorityDenied
from ..fixtures.native_child_artifact import materialize_managed_child_artifact
from ..runtime.providers.fakes import (
    FakeEffectAuthority,
    FakeRunner,
    _legacy_command_service,
)


class _AutoGrantAuthority(FakeEffectAuthority):
    """Owner-policy test double that grants each exact immutable intent."""

    def resolve(self, intent):
        try:
            return super().resolve(intent)
        except EffectAuthorityDenied:
            self.authorize(intent)
            return super().resolve(intent)
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import (
    CanonicalCompiledBundle,
    CatalogFile,
    ChildWorkflowContract,
    ResolvedCatalog,
    ResolvedChild,
    validate_semantics,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def _materialized(tmp_path: Path):
    store = RecipeBundleStore(tmp_path / "recipe-authority")
    return (
        StrictRecipeIngress(FIXTURES)
        .inspect("parent_direct.recipe.yaml")
        .authorize(RecipeAuthorityPolicy())
        .capture(store)
        .materialize(store)
    )


def test_async_direct_child_pause_uses_the_native_async_resume_port(
    tmp_path: Path,
) -> None:
    """Catches routing async child resume through a synchronous surrogate."""
    async def run():
        app = yg.open_native_app(_materialized(tmp_path))
        parked = await app.ainvoke({}, thread_id="async-parent")
        coordinate = parked.pending[0].coordinate
        completed = await app.aresume(
            thread_id="async-parent",
            results_by_interrupt_id={coordinate.interrupt_id: "yes"},
        )
        app.close()
        return completed

    completed = asyncio.run(run())

    assert completed.pending == ()
    assert completed.values["phase"] == "complete"


def test_compiler_generated_direct_child_pauses_and_resumes_after_sqlite_restart(
    tmp_path: Path,
) -> None:
    """Catches generated composition that only looks direct but cannot execute natively."""
    child_bytes = (
        b"version: '1.0'\nname: child\nstate: {answer: str, lockstep_outcome: str}\n"
        b"nodes:\n"
        b"  ask:\n    type: interrupt\n    message:\n      step: child\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: child\n        runner: null\n"
        b"        inputs: {}\n        writes: []\n        artifacts: []\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"    state_key: question\n    resume_key: answer\n    idempotent: false\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: ask}, {from: ask, to: pass}, {from: pass, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    contract = ChildWorkflowContract(("pass", "fail", "error"))
    catalog = ResolvedCatalog(
        children={
            "child": ResolvedChild(
                "child",
                contract,
                "1" * 64,
                CanonicalCompiledBundle.build(
                    root_relative_path="child.recipe.yaml",
                    files=(child_file,),
                    compiler_version="1",
                ),
            )
        }
    )
    source = tmp_path / "parent.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: parent\ndescription: native child\n"
        "protect: ['**']\nflow:\n"
        "  - call:\n      id: child-call\n      workflow: child\n"
        "      runner: codex\n      timeout_minutes: 5\n"
    )
    workflow = parse_workflow(load_workflow(source))
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    root = tmp_path / result.root_relative_path
    root.write_bytes(result.recipe_bytes)
    for generated in result.generated_files:
        target = tmp_path / generated.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generated.content)
    database = tmp_path / "native.sqlite"

    first = yg._open_native_path(root, database)  # noqa: SLF001 - integration oracle
    scope_parked = first.invoke({}, thread_id="compiled-child")
    first.close()
    second = yg._open_native_path(root, database)  # noqa: SLF001 - integration oracle
    child_parked = second.resume(
        thread_id="compiled-child",
        results_by_interrupt_id={
            scope_parked.pending[0].coordinate.interrupt_id: {"outcome": "PASS"}
        },
    )
    second.close()
    third = yg._open_native_path(root, database)  # noqa: SLF001 - integration oracle
    completed = third.resume(
        thread_id="compiled-child",
        results_by_interrupt_id={
            child_parked.pending[0].coordinate.interrupt_id: {"outcome": "PASS"}
        },
    )
    third.close()

    assert completed.pending == ()
    assert completed.values["lockstep_outcome"] == "PASS"


def test_child_artifact_ref_bridge_survives_restart_before_publish(
    tmp_path: Path,
) -> None:
    """Catches exporting a live path or losing the delivered result at restart."""
    fixture = materialize_managed_child_artifact(tmp_path)
    database = tmp_path / "artifact-native.sqlite"
    root = fixture.root_recipe

    first = yg._open_native_path(root, database)  # noqa: SLF001 - restart oracle
    scope_parked = first.invoke({}, thread_id="artifact-child")
    first.close()
    second = yg._open_native_path(root, database)  # noqa: SLF001 - restart oracle
    child_parked = second.resume(
        thread_id="artifact-child",
        results_by_interrupt_id={
            scope_parked.pending[0].coordinate.interrupt_id: {"outcome": "PASS"}
        },
    )
    child_interrupt = child_parked.pending[0]
    child_descriptor = parse_effect_descriptor(
        child_interrupt.value["lockstep_effect"]
    )
    child_result = {
        "schema": "lockstep.effect-result/v1",
        "effect_id": derive_effect_id(
            child_interrupt.coordinate, child_descriptor.digest
        ),
        "outcome": "PASS",
        "result_ref": "blob:" + "b" * 64,
        "artifact_refs": ["artifact:" + "a" * 64],
        "snapshot_ref": "snapshot:" + "c" * 64,
        "diff_ref": None,
        "fixed_error_code": None,
        "evidence_refs": [],
    }
    second.close()

    third = yg._open_native_path(root, database)  # noqa: SLF001 - restart oracle
    accept_parked = third.resume(
        thread_id="artifact-child",
        results_by_interrupt_id={
            child_interrupt.coordinate.interrupt_id: child_result
        },
    )
    third.close()

    assert len(accept_parked.pending) == 1
    accept = accept_parked.pending[0]
    assert accept.value["lockstep_effect"]["kind"] == "accept"
    assert accept.state_values is not None
    assert child_result in accept.state_values.values()
    accept_descriptor = parse_effect_descriptor(accept.value["lockstep_effect"])
    acceptance_result = {
        "schema": "lockstep.acceptance-result/v1",
        "effect_id": derive_effect_id(accept.coordinate, accept_descriptor.digest),
        "outcome": "PASS",
        "artifact_ref": "artifact:" + "a" * 64,
        "artifact_digest": "a" * 64,
        "consent_ref": "consent:review-call",
        "approval_generation": 1,
    }

    fourth = yg._open_native_path(root, database)  # noqa: SLF001 - restart oracle
    publish_parked = fourth.resume(
        thread_id="artifact-child",
        results_by_interrupt_id={accept.coordinate.interrupt_id: acceptance_result},
    )
    fourth.close()

    assert len(publish_parked.pending) == 1
    publish = publish_parked.pending[0]
    assert publish.value["lockstep_effect"]["kind"] == "publish"
    assert publish.state_values is not None
    assert child_result in publish.state_values.values()
    assert acceptance_result in publish.state_values.values()


def test_compiler_bundle_enters_service_with_sealed_scope_and_survives_restart(
    tmp_path: Path,
) -> None:
    """Proves admission, scope sealing, runner binding, and restart use one runtime."""
    child_bytes = (
        b"version: '1.0'\nname: child\nstate: {lockstep_outcome: str}\n"
        b"nodes:\n  work:\n    type: interrupt\n    state_key: request\n"
        b"    resume_key: result\n    idempotent: false\n    message:\n"
        b"      step: work\n      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: work\n        runner: null\n"
        b"        inputs: {}\n        writes: []\n        artifacts: []\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: work}, {from: work, to: pass}, {from: pass, to: END}]\n"
    )
    child_file = CatalogFile.build("child.recipe.yaml", child_bytes)
    catalog = ResolvedCatalog(children={
        "child": ResolvedChild(
            "child",
            ChildWorkflowContract(("pass", "fail", "error")),
            "4" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="child.recipe.yaml",
                files=(child_file,), compiler_version="1",
            ),
        )
    })
    source = tmp_path / "service-parent.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: service-parent\ndescription: service child\n"
        "protect: ['**']\nflow:\n"
        "  - call:\n      workflow: child\n      runner: codex\n"
        "      timeout_minutes: 5\n"
    )
    workflow = parse_workflow(load_workflow(source))
    result = compile_workflow(validate_semantics(workflow, catalog), catalog)
    recipes = tmp_path / "recipes"
    for relative_path, content in result.executable_files.items():
        target = recipes / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    project = tmp_path / "project"
    project.mkdir()
    runner = FakeRunner()
    authority = _AutoGrantAuthority()
    state = tmp_path / "state"

    first = _legacy_command_service(
        state, recipes, runners={"codex": runner}, effect_authority=authority
    )
    first._activate_writable_core()  # noqa: SLF001 - deterministic test fixture
    first._pump_stop.set()  # noqa: SLF001 - deterministic coordinator oracle
    first._pump_wakeup.set()  # noqa: SLF001
    first._pump_thread.join(timeout=5)  # noqa: SLF001
    started = first.start(
        "service-parent",
        {},
        str(project),
        compiler_provenance=result.compiler_provenance,
    )
    binding = first.catalog.get(started["run_id"])
    for _ in range(16):
        report = first.coordinator.reconcile(binding.public_run_id)
        if report.action == "awaiting_delivery":
            first.coordinator.deliver_ready(binding.public_run_id)
        if runner.prepare_calls:
            break
    snapshot = first.runtime.snapshot(binding.public_run_id, subgraphs=True)
    scope_key = next(key for key in snapshot.values if key.endswith("_scope_result"))
    scope = parse_scope_result(snapshot.values[scope_key])
    assert scope.runner_binding_digest == runner.binding_digest
    assert scope.absolute_deadline is not None
    assert runner.prepare_calls
    assert runner.prepare_calls[0].runner_binding_digest == runner.binding_digest
    assert runner.prepare_calls[0].deadline_at == scope.absolute_deadline
    projection = Engine.observe(first.state_dir, first.recipes_dir)
    projected = projection.status(binding.public_run_id, str(project))
    child_run_id = projected["child_run_id"]
    assert child_run_id.startswith("child-")
    with pytest.raises(LockstepError, match="unknown run"):
        projection.status(child_run_id, str(project))
    with pytest.raises(LockstepError, match="unknown run"):
        projection.history(child_run_id, str(project))
    with pytest.raises(LockstepError, match="unknown run"):
        first.scenario_done(
            child_run_id,
            "work",
            {},
            session_id=None,
            project=str(project),
        )
    with pytest.raises(KeyError):
        first.catalog.get(child_run_id)
    first.close()

    restarted = _legacy_command_service(
        state, recipes, runners={"codex": runner}, effect_authority=authority
    )
    restarted._activate_writable_core()  # noqa: SLF001 - restart fixture
    after = restarted.runtime.snapshot(binding.public_run_id, subgraphs=True)
    restarted.close()
    assert parse_scope_result(after.values[scope_key]) == scope
    assert len(runner.ensure_started_calls) >= 1


def test_nested_direct_child_uses_inner_runner_and_minimum_ancestor_deadline(
    tmp_path: Path,
) -> None:
    leaf_bytes = (
        b"version: '1.0'\nname: leaf\nstate: {brief: str, lockstep_outcome: str}\n"
        b"nodes:\n  work:\n    type: interrupt\n    state_key: request\n"
        b"    resume_key: result\n    idempotent: false\n    message:\n"
        b"      lockstep_effect:\n        schema: lockstep.effect/v1\n"
        b"        kind: manual\n        logical_id: work\n        runner: null\n"
        b"        inputs: {brief: {state_key: brief}}\n"
        b"        writes: []\n        artifacts: []\n"
        b"        deadline_seconds: null\n        scope_state_keys: []\n"
        b"        result_schema: lockstep.effect-result/v1\n"
        b"  pass: {type: passthrough, output: {lockstep_outcome: PASS}}\n"
        b"edges: [{from: START, to: work}, {from: work, to: pass}, {from: pass, to: END}]\n"
    )
    leaf_file = CatalogFile.build("leaf.recipe.yaml", leaf_bytes)
    leaf = ResolvedChild(
        "leaf",
        ChildWorkflowContract(
            ("pass", "fail", "error"), state_inputs={"brief": "str"}
        ),
        "c" * 64,
        CanonicalCompiledBundle.build(
            root_relative_path="leaf.recipe.yaml",
            files=(leaf_file,),
            compiler_version="1",
        ),
    )

    def workflow_source(name: str, flow: str):
        path = tmp_path / f"{name}.workflow.yaml"
        path.write_text(
            "workflow_version: '1'\n"
            f"name: {name}\ndescription: {name}\nprotect: ['**']\nflow:\n{flow}"
        )
        return parse_workflow(load_workflow(path))

    child_catalog = ResolvedCatalog(children={"leaf": leaf})
    child_ir = workflow_source(
        "child",
        "  - call:\n      workflow: leaf\n      runner: pinned\n"
        "      timeout_minutes: 10\n",
    )
    child_result = compile_workflow(
        validate_semantics(child_ir, child_catalog), child_catalog
    )
    child = ResolvedChild(
        "child",
        ChildWorkflowContract(
            ("pass", "fail", "error"), state_inputs={"brief": "str"}
        ),
        child_ir.source_sha256,
        child_result.as_catalog_bundle(),
    )
    parent_catalog = ResolvedCatalog(children={"child": child})
    parent_ir = workflow_source(
        "parent",
        "  - call:\n      workflow: child\n      runner: codex\n"
        "      timeout_minutes: 5\n",
    )
    result = compile_workflow(
        validate_semantics(parent_ir, parent_catalog), parent_catalog
    )
    recipes = tmp_path / "nested-recipes"
    for relative_path, content in result.executable_files.items():
        target = recipes / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    project = tmp_path / "nested-project"
    project.mkdir()
    codex = FakeRunner(binding_digest="d" * 64)
    reviewer = FakeRunner(binding_digest="e" * 64)
    authority = _AutoGrantAuthority()
    service = _legacy_command_service(
        tmp_path / "nested-state",
        recipes,
        runners={"codex": codex, "pinned": reviewer},
        effect_authority=authority,
    )
    service._activate_writable_core()  # noqa: SLF001 - deterministic test fixture
    service._pump_stop.set()  # noqa: SLF001 - deterministic coordinator oracle
    service._pump_wakeup.set()  # noqa: SLF001
    service._pump_thread.join(timeout=5)  # noqa: SLF001
    started = service.start(
        "parent",
        {"brief": "consumer-local-brief"},
        str(project),
        compiler_provenance=result.compiler_provenance,
    )
    for _ in range(32):
        report = service.coordinator.reconcile(started["run_id"])
        if report.action == "awaiting_delivery":
            service.coordinator.deliver_ready(started["run_id"])
        if reviewer.prepare_calls:
            break
    snapshot = service.runtime.snapshot(started["run_id"], subgraphs=True)
    value_maps = [snapshot.values]
    value_maps.extend(
        dict(interrupt.state_values)
        for interrupt in snapshot.pending
        if interrupt.state_values is not None
    )
    scopes = [
        parse_scope_result(value)
        for values in value_maps
        for value in values.values()
        if isinstance(value, dict)
        and value.get("schema") == "lockstep.scope-result/v1"
    ]
    service.close()

    outer = next(scope for scope in scopes if scope.runner_selector == "codex")
    inner = next(scope for scope in scopes if scope.runner_selector == "pinned")
    assert inner.absolute_deadline is not None
    assert outer.absolute_deadline is not None
    assert inner.absolute_deadline <= outer.absolute_deadline
    assert inner.runner_binding_digest == reviewer.binding_digest
    assert reviewer.prepare_calls[0].runner_binding_digest == reviewer.binding_digest
    assert reviewer.prepare_calls[0].deadline_at == inner.absolute_deadline
    assert reviewer.prepare_calls[0].inputs == (
        ("brief", "consumer-local-brief"),
    )
    assert codex.prepare_calls == []
