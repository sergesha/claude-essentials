"""Task 12 B0.5 real acceptance-lifetime RED freeze."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import lockstep.runtime.service as service_module
from lockstep.runtime.artifacts import ArtifactRecord
from lockstep.runtime.effects.descriptors import parse_effect_result
from lockstep.runtime.effects.owner_policy import OwnerRuntimeSnapshot
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.graph_runtime import GraphRuntime
from lockstep.runtime.providers.base import (
    EffectRequest,
    PreparedLaunch,
    TerminalSafetyObservation,
)
from lockstep.runtime.runtime_execution import OwnerRuntimeEffectAuthority
from lockstep.runtime.service import LockstepCommandService
from tests.fixtures.native_child_artifact import materialize_managed_child_artifact
from tests.runtime._runtime_commitment_harness import (
    ProvisionedRuntimeClosure,
    provision_compiled_managed_closure,
)
from tests.runtime.providers.fakes import FakeRunner


@dataclass(frozen=True, slots=True)
class _ManagedCompletion:
    producer_effect_id: str
    rollover_digest: str
    result_snapshot_ref: str


@dataclass(frozen=True, slots=True)
class _MaterializedAcceptance:
    artifact: ArtifactRecord
    accept_step: str


@dataclass(frozen=True, slots=True)
class _PendingArtifactAcceptance:
    provisioned: ProvisionedRuntimeClosure
    run_id: str
    thread_id: str
    producer_effect_id: str
    artifact: ArtifactRecord
    accept_step: str
    consent_token: str
    consent_digest: str
    owner_snapshot_digest: str
    owner_snapshot: OwnerRuntimeSnapshot


class _OwnerBoundFakeRunner(FakeRunner):
    """Return the real owner-authorized workspace in the fake launch record."""

    def prepare(self, request: EffectRequest) -> PreparedLaunch:
        self.workspace_refs.append(request.workspace_ref)
        return super().prepare(request)


def _stop_pump(command: LockstepCommandService) -> None:
    command._pump_stop.set()  # noqa: SLF001 - deterministic lifecycle boundary
    command._pump_wakeup.set()  # noqa: SLF001
    thread = command._pump_thread  # noqa: SLF001
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive()


def _watch_ids(command: LockstepCommandService) -> tuple[str, ...]:
    high_water = command.effects.max_run_drive_admission_seq()
    if high_water is None:
        return ()
    return tuple(
        watch.public_run_id
        for watch in command.effects.list_run_drive_watches(
            after_admission_seq=0,
            high_water=high_water,
            limit=128,
        )
    )


def _substitute_exact_binding_runner(monkeypatch) -> list[_OwnerBoundFakeRunner]:
    """Keep production context/authority and replace only the Codex adapter."""

    built = service_module.build_runtime_execution_composition
    runners: list[_OwnerBoundFakeRunner] = []

    def build(**kwargs):
        composition = built(**kwargs)
        runner = _OwnerBoundFakeRunner(
            binding_digest=kwargs["context"].snapshot.codex.binding_digest,
            required_authorities=composition.runners.codex.required_authorities,
        )
        runners.append(runner)
        return replace(
            composition,
            runners=replace(composition.runners, codex=runner),
        )

    monkeypatch.setattr(
        service_module,
        "build_runtime_execution_composition",
        build,
    )
    return runners


def _complete_managed_attempt(
    command: LockstepCommandService,
    runner: _OwnerBoundFakeRunner,
    run_id: str,
) -> _ManagedCompletion:
    """Complete the exact real managed attempt and drive native state once."""

    binding = command.catalog.get(run_id)
    command.runtime.bind(binding)
    records = command.effects.list_for_thread(binding.thread_id)
    assert records, command.runtime.snapshot(run_id, subgraphs=True)
    managed_records = tuple(
        record
        for record in records
        if record.effect_kind == "managed"
    )
    assert len(managed_records) == 1
    producer = managed_records[0]
    managed_interrupts = tuple(
        (interrupt, descriptor)
        for interrupt in command.runtime.snapshot(run_id, subgraphs=True).pending
        if (
            descriptor := command._protected_interrupt_descriptor(  # noqa: SLF001
                interrupt
            )
        )
        is not None
        and descriptor.kind == "managed"
    )
    assert len(managed_interrupts) == 1
    managed_interrupt, _managed_descriptor = managed_interrupts[0]
    runtime_input = command.snapshot_resolver._current_ref(  # noqa: SLF001
        binding,
        managed_interrupt,
    )
    assert producer.phase == "running"
    assert producer.runner_binding_digest == runner.binding_digest

    rollover = command.snapshots.capture(
        {"review.md": command.blobs.put(b"APPROVED\n")},
        declared_paths=("review.md",),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": producer.request_digest,
            "workspace_ref": producer.workspace_ref,
        },
        previous=runtime_input,
    )
    launch = runner.ensure_started_calls[-1]
    result = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": producer.effect_id,
            "outcome": "PASS",
            "result_ref": "blob:" + "d" * 64,
            "artifact_refs": [],
            "snapshot_ref": f"snapshot:{rollover.digest}",
            "diff_ref": None,
            "fixed_error_code": None,
            "evidence_refs": [],
        }
    )
    runner.inspect_observations.append(runner.terminal(launch, result))
    runner.safety_observations.append(
        TerminalSafetyObservation.proven_for(
            launch,
            rollover_snapshot_ref=result.snapshot_ref,
            result_stable=True,
        )
    )
    status = command._drive_engine_owned(run_id)  # noqa: SLF001 - exact crash cut
    assert (status.status, status.owner) == ("running", "engine")
    assert result.snapshot_ref is not None
    return _ManagedCompletion(
        producer.effect_id,
        rollover.digest,
        result.snapshot_ref,
    )


def _read_materialized_artifact(
    command: LockstepCommandService,
    run_id: str,
    completion: _ManagedCompletion,
) -> _MaterializedAcceptance:
    """Read and prove the producer, artifact registry, and native acceptance."""

    binding = command.catalog.get(run_id)
    command.runtime.bind(binding)
    pending = command.runtime.snapshot(run_id, subgraphs=True)
    assert len(pending.pending) == 1
    descriptor = command._protected_interrupt_descriptor(  # noqa: SLF001
        pending.pending[0]
    )
    assert descriptor is not None
    assert descriptor.kind == "accept"
    producer = command.effects.get(completion.producer_effect_id)
    assert producer.phase == "delivered"
    assert producer.result is not None
    assert len(producer.result.artifact_refs) == 1
    artifact = command.artifacts.read(producer.result.artifact_refs[0])
    assert artifact.producer_effect_id == producer.effect_id
    assert artifact.producer_request_digest == producer.request_digest
    assert artifact.producer_coordinate == producer.coordinate
    assert artifact.public_run_id == run_id
    assert artifact.project_identity == binding.project_identity
    assert artifact.definition_digest == binding.recipe_digest
    assert producer.result.snapshot_ref == completion.result_snapshot_ref
    assert artifact.source_snapshot_ref.digest == completion.rollover_digest
    assert command.blobs.read(artifact.blob) == b"APPROVED\n"
    return _MaterializedAcceptance(artifact, descriptor.logical_id)


def _close_at_pending_artifact_acceptance(
    tmp_path: Path,
    monkeypatch,
) -> _PendingArtifactAcceptance:
    project = tmp_path / "project"
    project.mkdir()
    fixture = materialize_managed_child_artifact(project)
    provisioned = provision_compiled_managed_closure(
        tmp_path,
        monkeypatch,
        project=project,
        recipe="artifact-parent",
        compiler_provenance=fixture.compilation.compiler_provenance,
    )
    fake_runners = _substitute_exact_binding_runner(monkeypatch)
    owner_digest, owner_snapshot = open_runtime_snapshot(provisioned.owner_state)
    command = Engine.command(provisioned.owner_state, fixture.recipes_dir)
    try:
        _stop_pump(command)
        started = command.start(
            provisioned.recipe,
            {},
            str(project),
            compiler_provenance=fixture.compilation.compiler_provenance,
        )
        run_id = started["run_id"]
        command._recover_engine_effects()  # noqa: SLF001 - stopped pump's real sweep
        _stop_pump(command)
        binding = command.catalog.get(run_id)
        assert isinstance(command.runtime, GraphRuntime)
        assert command._runtime_execution_context.snapshot_digest == owner_digest
        assert command._runtime_execution_context.snapshot == owner_snapshot
        composition = command._runtime_execution_composition
        assert isinstance(composition.authority, OwnerRuntimeEffectAuthority)
        assert len(fake_runners) == 1
        assert composition.runners.codex is fake_runners[0]
        assert fake_runners[0].binding_digest == owner_snapshot.codex.binding_digest

        completion = _complete_managed_attempt(command, fake_runners[0], run_id)
        materialized = _read_materialized_artifact(command, run_id, completion)
        preview = command.preview_publication_consent(
            run_id,
            materialized.accept_step,
            project=str(project),
        )
        issued = command.issue_publication_consent(
            run_id,
            materialized.accept_step,
            preview["digest"],
            project=str(project),
        )
        assert (
            command.authority.inspect_token(issued.token).commitment.digest
            == preview["digest"]
        )
        return _PendingArtifactAcceptance(
            provisioned=provisioned,
            run_id=run_id,
            thread_id=binding.thread_id,
            producer_effect_id=completion.producer_effect_id,
            artifact=materialized.artifact,
            accept_step=materialized.accept_step,
            consent_token=issued.token,
            consent_digest=preview["digest"],
            owner_snapshot_digest=owner_digest,
            owner_snapshot=owner_snapshot,
        )
    finally:
        command.close()


def test_watch_survives_real_child_artifact_until_pending_acceptance_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Keep the admitted watch through real child acceptance and restart."""

    crash = _close_at_pending_artifact_acceptance(tmp_path, monkeypatch)
    restarted = Engine.command(
        crash.provisioned.owner_state,
        crash.provisioned.project / ".lockstep" / "recipes",
    )
    try:
        _stop_pump(restarted)
        restarted.scenario_recover(str(crash.provisioned.project), limit=128)
        _stop_pump(restarted)
        binding = restarted.catalog.get(crash.run_id)
        assert binding.thread_id == crash.thread_id
        assert isinstance(restarted.runtime, GraphRuntime)
        watch_ids = _watch_ids(restarted)
        if watch_ids:
            assert (
                restarted._runtime_execution_context.snapshot_digest
                == crash.owner_snapshot_digest
            )
            assert (
                restarted._runtime_execution_context.snapshot
                == crash.owner_snapshot
            )
            composition = restarted._runtime_execution_composition
            assert isinstance(composition.authority, OwnerRuntimeEffectAuthority)
            assert composition.runners.codex.binding_digest == (
                crash.owner_snapshot.codex.binding_digest
            )

        restarted.runtime.bind(binding)
        pending = restarted.runtime.snapshot(crash.run_id, subgraphs=True)
        assert len(pending.pending) == 1
        descriptor = restarted._protected_interrupt_descriptor(  # noqa: SLF001
            pending.pending[0]
        )
        assert descriptor is not None
        assert (descriptor.kind, descriptor.logical_id) == (
            "accept",
            crash.accept_step,
        )
        assert restarted.effects.get(crash.producer_effect_id).phase == "delivered"
        assert restarted.artifacts.read(str(crash.artifact.ref)) == crash.artifact
        assert (
            restarted.authority.inspect_token(crash.consent_token).commitment.digest
            == crash.consent_digest
        )
        assert watch_ids == (crash.run_id,)
    finally:
        restarted.close()
