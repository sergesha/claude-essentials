"""Task 12R1b-E RED freeze for public composition and commitment."""

from __future__ import annotations

from threading import Event

import pytest
import yaml

from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.effects.owner_policy import (
    RuntimeRequirementIndex,
    requirement_digest,
)
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.providers.base import EffectRequest, launch_commitment_digest
from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.pinned import PinnedRunnerAdapter
from lockstep.templates import install_template

from ._runtime_commitment_harness import provision_managed_closure


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

    granted: list[object] = []
    prepared: list[tuple[CodexRunnerAdapter, EffectRequest]] = []
    commitments: list[tuple[object, object, str, object]] = []
    commitment_reached = Event()
    command_holder: list[object] = []
    original_bind_grant = EffectRequest.bind_grant
    original_prepare = CodexRunnerAdapter.prepare
    original_ensure_started = CodexRunnerAdapter.ensure_started

    def capture_grant(intent, grant):
        granted.append(grant)
        return original_bind_grant(intent, grant)

    def capture_prepare(adapter, request):
        prepared.append((adapter, request))
        return original_prepare(adapter, request)

    def observe_durable_commitment(adapter, launch):
        assert len(prepared) == 1
        command_service = command_holder[0]
        record = command_service.effects.get(launch.effect_id)
        current_digest, current_snapshot = open_runtime_snapshot(
            provisioned.owner_state
        )
        commitments.append(
            (launch, record, current_digest, current_snapshot)
        )
        commitment_reached.set()
        return original_ensure_started(adapter, launch)

    monkeypatch.setattr(EffectRequest, "bind_grant", capture_grant)
    monkeypatch.setattr(CodexRunnerAdapter, "prepare", capture_prepare)
    monkeypatch.setattr(
        CodexRunnerAdapter,
        "ensure_started",
        observe_durable_commitment,
    )
    command = Engine.command(
        provisioned.owner_state,
        provisioned.project / ".lockstep" / "recipes",
    )
    command_holder.append(command)
    try:
        started = command.start(
            provisioned.recipe,
            {"brief": "review the bounded project snapshot"},
            str(provisioned.project),
        )
        assert started["run_id"]
        assert commitment_reached.wait(timeout=2), (
            "public managed start remained at the static-admission park instead "
            "of reaching its durable, current owner-guarded launch commitment"
        )
        assert len(commitments) == 1
        assert len(granted) == len(prepared) == 1
        effect_grant = granted[0]
        adapter, request = prepared[0]
        launch, launching_record, current_digest, current_snapshot = commitments[0]
        assert launching_record.phase == "launching"
        assert launching_record.launch_commitment_digest == (
            launch_commitment_digest(request, launch)
        )
        assert current_digest == _snapshot_digest
        assert current_snapshot == snapshot
        assert effect_grant.actor_binding_digest == owner_grant.requirement_digest
        assert effect_grant.config_epoch == snapshot.config_generation
        assert effect_grant.policy_epoch == snapshot.policy_generation
        assert effect_grant.grant_generation == owner_grant.grant_generation
        assert adapter.binding_digest == snapshot.codex.binding_digest
        assert request.runner_selector == "codex"
        assert request.runner_binding_digest == snapshot.codex.binding_digest
        assert request.grant_digest == effect_grant.digest
        record = command.effects.get(request.effect_id)
        assert record.runner_binding_digest == request.runner_binding_digest
        assert record.request_digest == request.request_digest
        assert record.grant_digest == effect_grant.digest
    finally:
        command.close()
