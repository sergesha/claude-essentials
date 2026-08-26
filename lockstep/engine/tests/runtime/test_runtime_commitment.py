"""Task 12R1b-E RED freeze for public composition and commitment."""

from __future__ import annotations

import os
import time
from contextlib import suppress

import pytest
import yaml

from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.effects.owner_policy import (
    RuntimeRequirementIndex,
    requirement_digest,
)
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.providers.base import launch_commitment_digest
from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.pinned import PinnedRunnerAdapter
from lockstep.runtime.recipe_bundles import RecipeBundleRef
from lockstep.templates import install_template

from ._runtime_commitment_harness import (
    ManagedRestartFifoBarrier,
    provision_managed_closure,
    provision_pinned_verify_closure,
)
from ._runtime_commitment_observer import RuntimeCommitmentObserver


def _await_provider_markers(provisioned, command, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (
            provisioned.provider_argv_marker.is_file()
            and provisioned.provider_environment_marker.is_file()
        ):
            return (
                tuple(provisioned.provider_argv_marker.read_text().splitlines()),
                tuple(
                    provisioned.provider_environment_marker.read_text().splitlines()
                ),
            )
        if command._pump_failure is not None:
            raise command._pump_failure
        time.sleep(0.02)
    pytest.fail("public lifecycle did not reach the owner-captured executable marker")


def _await_completed(provisioned, run_id: str, *, timeout: float = 10.0):
    projection = Engine.observe(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    deadline = time.monotonic() + timeout
    observed = projection.status(run_id, str(provisioned.project))
    while observed["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.02)
        observed = projection.status(run_id, str(provisioned.project))
    return observed


def _await_effect_phase(command, run_id: str, phase: str, *, timeout: float = 10.0):
    binding = command.catalog.get(run_id)
    deadline = time.monotonic() + timeout
    records = command.effects.list_for_thread(binding.thread_id)
    while (
        (len(records) != 1 or records[0].phase != phase)
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
        records = command.effects.list_for_thread(binding.thread_id)
    return records


def _assert_completed_delivery(command, provisioned, run_id: str):
    final_status = _await_completed(provisioned, run_id)
    records = _await_effect_phase(command, run_id, "delivered")
    assert final_status == {
        "status": "completed",
        "run_id": run_id,
        "owner": "engine",
        "next_action": None,
    }
    assert len(records) == 1
    record = records[0]
    assert record.phase == "delivered"
    assert record.result is not None
    assert record.result.outcome == "PASS"
    return record


@pytest.mark.parametrize(
    ("template", "expected_scope_rows"),
    [("reviewed-change", 1), ("parallel-review", 3)],
)
def test_packaged_template_scope_only_public_start_has_no_runtime_authority(
    template,
    expected_scope_rows,
    tmp_path,
    monkeypatch,
) -> None:
    """A5 GREEN: current packaged scope graphs require and launch no runner."""

    project = tmp_path / "project"
    project.mkdir()
    install_template(template, "release", project)
    recipes = project / ".lockstep" / "recipes"
    authorized = StrictRecipeIngress(recipes).inspect(
        "release.recipe.yaml"
    ).authorize(RecipeAuthorityPolicy())
    index = RuntimeRequirementIndex.for_authorized_closure(
        authorized,
        project_identity=str(project.resolve()),
    )
    assert index.requirements == ()
    declared_scopes = 0
    for item in authorized.files:
        document = yaml.safe_load(item.bytes)
        declared_scopes += sum(
            1
            for node in document.get("nodes", {}).values()
            if node.get("message", {}).get("lockstep_effect", {}).get("kind")
            == "scope"
        )
    assert declared_scopes == expected_scope_rows
    provider_requests: list[object] = []
    codex_prepare = CodexRunnerAdapter.prepare
    pinned_prepare = PinnedRunnerAdapter.prepare

    def observe_codex(adapter, request):
        provider_requests.append(request)
        return codex_prepare(adapter, request)

    def observe_pinned(adapter, request):
        provider_requests.append(request)
        return pinned_prepare(adapter, request)

    monkeypatch.setattr(CodexRunnerAdapter, "prepare", observe_codex)
    monkeypatch.setattr(PinnedRunnerAdapter, "prepare", observe_pinned)
    owner_state = tmp_path / "owner-state"
    command = Engine.command(owner_state, recipes)
    try:
        try:
            command.start("release", {}, str(project))
        except Exception:
            # Completion/availability is not this control's contract.  Before
            # R1b-E one template stops at unwired composition and the parallel
            # template can reach an unrelated native topology failure.  The
            # durable authority-absence oracles below must hold in either case.
            pass
        with command.store.read_connection() as connection:
            rows = connection.execute(command.store.tables.effects.select()).mappings()
            effects = tuple(rows)
        bindings = command.catalog.list(str(project.resolve()))
        assert len(bindings) == 1
        assert len(effects) <= expected_scope_rows
        assert {record["effect_kind"] for record in effects} <= {"scope"}
        assert all(record["effect_kind"] == "scope" for record in effects)
        assert all(record["runner_binding_digest"] is None for record in effects)
        assert all(record["request_digest"] is None for record in effects)
        assert all(record["grant_digest"] is None for record in effects)
        assert all(record["launch_commitment_digest"] is None for record in effects)
        assert provider_requests == []
        assert not (owner_state / "runtime-owner").exists()
    finally:
        command.close()


def test_public_managed_codex_binds_requirement_through_durable_commitment(
    tmp_path,
    monkeypatch,
) -> None:
    """A5 RED: exact owner authority must reach the released Codex request."""

    provisioned = provision_managed_closure(tmp_path, monkeypatch)
    _snapshot_digest, snapshot = open_runtime_snapshot(provisioned.owner_state)
    requirement = provisioned.requirement_index.requirements[0]
    owner_grant = snapshot.grants[0]
    assert owner_grant.grant_selection_key == requirement.grant_selection_key
    assert owner_grant.requirement_digest == requirement_digest(
        grant_selection_key=requirement.grant_selection_key,
        runner_binding_digest=snapshot.codex.binding_digest,
        config_generation=snapshot.config_generation,
    )

    observer = RuntimeCommitmentObserver(monkeypatch, provisioned.owner_state)
    command = Engine.command(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    observer.attach(command)
    try:
        started = command.start(
            provisioned.recipe,
            {"brief": "review the bounded project snapshot"},
            str(provisioned.project),
        )
        assert started["run_id"]
        assert observer.reached.wait(timeout=2), (
            "public managed start remained at the static-admission park instead "
            "of reaching its durable, current owner-guarded launch commitment"
        )
        assert observer.commitments
        assert observer.bound_requests
        assert observer.prepares
        reference_grant = observer.bound_requests[0].grant
        reference_request = observer.bound_requests[0].request
        reference_launch_digest = (
            observer.commitments[0].record.launch_commitment_digest
        )
        assert reference_launch_digest is not None
        for call in observer.bound_requests:
            intent, effect_grant, bound_request = (
                call.intent,
                call.grant,
                call.request,
            )
            assert effect_grant.actor_binding_digest == owner_grant.requirement_digest
            assert effect_grant.config_epoch == snapshot.config_generation
            assert effect_grant.policy_epoch == snapshot.policy_generation
            assert effect_grant.grant_generation == owner_grant.grant_generation
            assert (
                effect_grant.parent_capability_generation
                == owner_grant.grant_generation
            )
            assert effect_grant.digest == reference_grant.digest
            assert bound_request.effect_id == reference_request.effect_id
            assert bound_request.request_digest == reference_request.request_digest
            assert bound_request.runner_binding_digest == snapshot.codex.binding_digest
            assert bound_request.grant_digest == effect_grant.digest
            assert intent.effect_id == bound_request.effect_id
            assert intent.intent_digest == bound_request.intent_digest
        for call in observer.prepares:
            adapter, request, launch = call.adapter, call.request, call.launch
            assert adapter.binding_digest == snapshot.codex.binding_digest
            assert request.runner_selector == "codex"
            assert request.runner_binding_digest == snapshot.codex.binding_digest
            assert request.request_digest == reference_request.request_digest
            assert request.grant_digest == reference_grant.digest
            assert launch.effect_id == request.effect_id
            assert launch.request_digest == request.request_digest
            assert launch.runner_binding_digest == request.runner_binding_digest
            assert (
                launch_commitment_digest(request, launch)
                == reference_launch_digest
            )
        for call in observer.commitments:
            assert call.correlated_prepares
            launching_record = call.record
            assert launching_record.phase == "launching"
            assert launching_record.launch_commitment_digest == (
                reference_launch_digest
            )
            assert all(
                launching_record.launch_commitment_digest
                == launch_commitment_digest(prepared.request, call.launch)
                for prepared in call.correlated_prepares
            )
            assert launching_record.runner_binding_digest == (
                snapshot.codex.binding_digest
            )
            assert launching_record.request_digest == reference_request.request_digest
            assert launching_record.grant_digest == reference_grant.digest
            assert call.owner_digest == _snapshot_digest
            assert call.owner_snapshot == snapshot
    finally:
        observer.release()
        command.close()


def test_active_command_installs_first_protected_runtime_composition(
    tmp_path,
    monkeypatch,
) -> None:
    """A5 RED: prior ordinary activation must not poison protected starts."""

    provisioned = provision_managed_closure(tmp_path, monkeypatch)
    command = Engine.command(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    observer = RuntimeCommitmentObserver(monkeypatch, provisioned.owner_state)
    observer.attach(command)
    try:
        assert command.scenario_recover(str(provisioned.project)) == {
            "recovered": [],
            "count": 0,
            "limit": 128,
        }
        started = command.start(
            provisioned.recipe,
            {"brief": "review after an ordinary command activation"},
            str(provisioned.project),
        )
        assert started["run_id"]
        assert observer.reached.wait(timeout=2), (
            "the active command capability did not install its first exact "
            "protected runtime composition"
        )
        assert observer.commitments
    finally:
        observer.release()
        command.close()


def test_public_managed_codex_runs_the_production_lifecycle_to_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    """A6 GREEN: public Codex work completes its real managed lifecycle."""

    provisioned = provision_managed_closure(tmp_path, monkeypatch)
    command = Engine.command(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    try:
        started = command.start(
            provisioned.recipe,
            {"brief": "complete the production managed lifecycle"},
            str(provisioned.project),
        )
        run_id = started["run_id"]

        actual_argv, actual_environment = _await_provider_markers(
            provisioned, command
        )
        assert actual_environment == (
            str(provisioned.codex_home),
            str(provisioned.codex_home),
        )

        record = _assert_completed_delivery(command, provisioned, run_id)
        launch = command._runtime_execution_composition.runners.codex.launch_record(
            record.effect_id
        )
        assert actual_argv == (
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--model",
            "task12-r1be-test-model",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-C",
            str(launch.workspace_path),
            "-",
        )
        assert launch.inner_argv == (str(tmp_path / "codex"), *actual_argv)
        assert dict(launch.environment) == {
            "CODEX_HOME": str(provisioned.codex_home),
            "HOME": str(provisioned.codex_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(tmp_path / "private-tmp"),
        }
        assert record.effect_kind == "managed"
        assert record.result.result_ref is not None
        assert record.result.snapshot_ref is not None
    finally:
        command.close()


def test_public_verify_uses_pinned_profile_and_credential_free_lifecycle(
    tmp_path,
    monkeypatch,
) -> None:
    """A7 GREEN: public verify uses the exact pinned lifecycle and binding."""

    provisioned = provision_pinned_verify_closure(tmp_path, monkeypatch)
    assert tuple(provisioned.pinned_home.iterdir()) == ()
    command = Engine.command(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    try:
        started = command.start(
            provisioned.recipe,
            {},
            str(provisioned.project),
        )
        run_id = started["run_id"]

        actual_argv, actual_environment = _await_provider_markers(
            provisioned, command
        )
        assert actual_environment == (
            str(provisioned.pinned_home),
            str(provisioned.pinned_home),
        )
        assert str(provisioned.codex_home) not in "\n".join(
            (*actual_argv, *actual_environment)
        )
        assert not (provisioned.pinned_home / "auth.json").exists()

        record = _assert_completed_delivery(command, provisioned, run_id)
        launch = command._runtime_execution_composition.runners.pinned.launch_record(
            record.effect_id
        )
        assert actual_argv == (
            "sandbox",
            "--permission-profile",
            "task12-pinned-profile",
            "--cd",
            str(launch.workspace_path),
            "--include-managed-config",
            "--",
            "python",
            "-m",
            "pytest",
            "-q",
        )
        assert launch.inner_argv == (str(tmp_path / "codex"), *actual_argv)
        assert dict(launch.environment) == {
            "CODEX_HOME": str(provisioned.pinned_home),
            "HOME": str(provisioned.pinned_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(tmp_path / "private-tmp"),
        }
        assert record.effect_kind == "verify"
        assert record.result.result_ref is None
        assert record.result.snapshot_ref is None
        assert record.workspace_ref is not None
    finally:
        command.close()


def test_public_managed_lifecycle_reconstructs_after_command_restart(
    tmp_path,
    monkeypatch,
) -> None:
    """A15 RED: restart must reconstruct and continue one admitted effect."""

    provisioned = provision_managed_closure(tmp_path, monkeypatch)
    barrier = ManagedRestartFifoBarrier.install(provisioned)
    command = Engine.command(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    restarted = None
    spawned = False
    terminal_path = None
    try:
        started = command.start(
            provisioned.recipe,
            {"brief": "continue after a real command-service restart"},
            str(provisioned.project),
        )
        run_id = started["run_id"]
        binding = command.catalog.get(run_id)
        records = _await_effect_phase(command, run_id, "running")
        assert len(records) == 1
        running = records[0]
        assert running.phase == "running"
        runner = command._runtime_execution_composition.runners.codex
        assert runner.spawn_count == 1
        spawned = True
        terminal_path = runner._directory(running.effect_id) / "terminal.json"
        snapshot_digest, owner_snapshot = open_runtime_snapshot(
            provisioned.owner_state
        )
        assert command._runtime_execution_context.snapshot_digest == snapshot_digest
        assert command._runtime_execution_context.snapshot == owner_snapshot

        bundle_ref = RecipeBundleRef(binding.recipe_snapshot_ref)
        manifest = command.bundle_store.read_manifest(bundle_ref)
        materialized = command.bundle_store.read_materialization(bundle_ref)
        assert manifest.root == f"{provisioned.recipe}.recipe.yaml"
        assert (materialized.directory / manifest.root).is_file()
        live_recipe = (
            provisioned.project
            / ".lockstep"
            / "recipes"
            / manifest.root
        )
        live_recipe.unlink()
        assert not live_recipe.exists()
        command.close()

        restarted = Engine.command(
            provisioned.owner_state,
            provisioned.project / ".lockstep" / "recipes",
        )
        restarted.scenario_recover(str(provisioned.project), limit=128)

        composition = restarted._runtime_execution_composition
        assert composition is not None
        assert composition.runners.codex.spawn_count == 0
        assert (
            composition.runners.codex.binding_digest
            == running.runner_binding_digest
        )
        assert restarted._runtime_execution_context.snapshot_digest == snapshot_digest
        assert restarted._runtime_execution_context.snapshot == owner_snapshot
        actual_argv, actual_environment, terminal = barrier.release(terminal_path)
        assert actual_argv
        assert actual_environment == (
            str(provisioned.codex_home),
            str(provisioned.codex_home),
        )
        assert terminal is True

        delivered = _assert_completed_delivery(restarted, provisioned, run_id)
        assert delivered.effect_id == running.effect_id
        assert delivered.effect_kind == "managed"
        assert delivered.result.result_ref is not None
        assert delivered.result.snapshot_ref is not None
        assert restarted.catalog.get(run_id) == binding
        assert restarted.bundle_store.read_manifest(bundle_ref) == manifest
        assert not live_recipe.exists()
    finally:
        command.close()
        if spawned and terminal_path is not None:
            with suppress(Exception):
                barrier.release(terminal_path)
        if restarted is not None:
            restarted.close()
