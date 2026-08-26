"""Task 12R1b-E RED freeze for public composition and commitment."""

from __future__ import annotations

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
from lockstep.templates import install_template

from ._runtime_commitment_harness import provision_managed_closure
from ._runtime_commitment_observer import RuntimeCommitmentObserver


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
