from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.authority import EffectGrant
from lockstep.runtime.effects.owner_consent import PublicationConsentCommitment
from lockstep.runtime.effects.owner_policy import (
    RuntimeProvisioningInventory,
    RuntimeRequirementIndex,
)
from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore
from lockstep.runtime.providers.base import EffectRequest
from lockstep.runtime.providers.codex import (
    CodexInstallationBinding,
    CodexLaunchDecisionGate,
    CodexRunnerAdapter,
    CodexSandboxAttestor,
)
from lockstep.runtime.providers.workspaces import LocalGitWorkspaceProvider
from lockstep.runtime.service import preflight_recipe
from lockstep.templates import install_template
from tests._managed_projection_fixture import managed_projection_compile
from tests.runtime._runtime_commitment_harness import _runtime_config
from tests.runtime._runtime_commitment_observer import RuntimeCommitmentObserver

CONTROLLED_EFFECT = (
    Path(__file__).parents[1] / "fixtures" / "controlled_effect_executable.py"
)


def _wait_for_public_worker_step(
    command,
    owner_state: Path,
    recipes: Path,
    project: Path,
    run_id: str,
    step: str,
    *,
    timeout: float = 20.0,
) -> dict[str, object]:
    projection = Engine.observe(owner_state, recipes)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if command._pump_failure is not None:
                raise command._pump_failure
            observed = projection.status(run_id, str(project))
            if (
                observed.get("status") == "awaiting"
                and observed.get("owner") == "worker"
                and observed.get("step") == step
            ):
                return observed
            time.sleep(0.02)
    finally:
        projection.close()
    pytest.fail(f"public lifecycle did not reach worker step {step!r}")


def _wait_for_public_terminal(
    command, project: Path, run_id: str, *, timeout: float = 20.0
) -> dict[str, object]:
    projection = Engine.observe(command.state_dir, command.recipes_dir)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if command._pump_failure is not None:
                raise command._pump_failure
            try:
                observed = projection.status(run_id, str(project))
            except LockstepError:
                time.sleep(0.02)
                continue
            if observed.get("status") == "completed":
                return observed
            time.sleep(0.02)
    finally:
        projection.close()
    pytest.fail("public lifecycle did not reach terminal completion")


def _public_compiled_managed_closure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "tracked.txt").write_bytes(b"exact run-start snapshot bytes\n")
    _child, compiled = managed_projection_compile(tmp_path)
    recipes = project / ".lockstep" / "recipes"
    for relative_path, content in compiled.executable_files.items():
        target = recipes / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    authorized = preflight_recipe(
        recipes,
        "managed-parent",
        compiler_provenance=compiled.compiler_provenance,
    )
    index = RuntimeRequirementIndex.for_authorized_closures(
        (authorized,), project_identity=str(project.resolve())
    )
    assert len(index.requirements) == 1

    config = _runtime_config(tmp_path)
    for selector in ("codex", "pinned"):
        binding = config[selector]
        assert isinstance(binding, dict)
        binding["executable"] = str(CONTROLLED_EFFECT)
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    codex = config["codex"]
    pinned = config["pinned"]
    assert isinstance(codex, dict)
    assert isinstance(pinned, dict)
    provision_runtime_snapshot(
        state_dir=owner_state,
        codex=codex,
        pinned=pinned,
        replacement_keys=tuple(
            requirement.grant_selection_key for requirement in index.requirements
        ),
        index=index,
        project=project,
    )
    return project, recipes, owner_state, compiled


def _installed_packaged_reviewed_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    shared_owner_state: Path | None = None,
):
    project = tmp_path / "reviewed-project"
    project.mkdir()
    install_template(
        "reviewed-change",
        "release",
        project,
        state_dir=tmp_path / "template-owner-state",
    )
    recipes = project / ".lockstep" / "recipes"
    authorized = preflight_recipe(recipes, "release")
    index = RuntimeRequirementIndex.for_authorized_closures(
        (authorized,), project_identity=str(project.resolve())
    )
    assert tuple(
        sorted(requirement.runner_selector for requirement in index.requirements)
    ) == ("codex", "pinned")

    owner_state = shared_owner_state or tmp_path / "reviewed-owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    return project, recipes, owner_state, index


def _provision_reviewed_inventory(
    runtime_root: Path,
    *,
    owner_state: Path,
    index: RuntimeRequirementIndex | RuntimeProvisioningInventory,
    project: Path,
) -> None:
    runtime_root.mkdir()
    config = _runtime_config(runtime_root)
    for selector in ("codex", "pinned"):
        binding = config[selector]
        assert isinstance(binding, dict)
        binding["executable"] = str(CONTROLLED_EFFECT)
    codex = config["codex"]
    pinned = config["pinned"]
    assert isinstance(codex, dict)
    assert isinstance(pinned, dict)
    provision_runtime_snapshot(
        state_dir=owner_state,
        codex=codex,
        pinned=pinned,
        replacement_keys=tuple(
            requirement.grant_selection_key for requirement in index.requirements
        ),
        index=index,
        project=project,
    )


def _public_packaged_reviewed_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    shared_owner_state: Path | None = None,
):
    project, recipes, owner_state, index = _installed_packaged_reviewed_closure(
        tmp_path,
        monkeypatch,
        shared_owner_state=shared_owner_state,
    )
    _provision_reviewed_inventory(
        tmp_path / "reviewed-runtime",
        owner_state=owner_state,
        index=index,
        project=project,
    )
    return project, recipes, owner_state


def test_public_compiled_managed_step_reaches_real_codex_commit_and_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, recipes, owner_state, compiled = _public_compiled_managed_closure(
        tmp_path, monkeypatch
    )
    expected_brief = (
        "Task:\nReview the implementation for correctness.\n\n"
        "Exit criterion:\nRecord findings and a final verdict.\n\n"
        "Artifact path: review.md\n"
        "Requested Markdown headings: Findings, Verdict\n"
    )
    observer = RuntimeCommitmentObserver(monkeypatch, owner_state)
    command = Engine.command(owner_state, recipes)
    observer.attach(command)
    released = False
    try:
        started = command.start(
            "managed-parent",
            {},
            str(project),
            compiler_provenance=compiled.compiler_provenance,
        )
        assert started["run_id"]
        deadline = time.monotonic() + 3
        while (
            not observer.reached.is_set()
            and command._pump_failure is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if command._pump_failure is not None:
            raise command._pump_failure
        assert observer.reached.is_set(), (
            "public compiled managed start did not reach the durable Codex "
            "launch commitment"
        )
        assert len(observer.commitments) == 1
        assert observer.prepares
        prepared = observer.prepares[0]
        commitment = observer.commitments[0]
        assert all(call == prepared for call in observer.prepares)
        request = prepared.request
        assert type(prepared.adapter) is CodexRunnerAdapter
        assert dict(request.inputs)["brief"].encode("utf-8") == expected_brief.encode(
            "utf-8"
        )
        snapshot_value = dict(request.inputs)["snapshot"]
        assert snapshot_value.startswith("snapshot:")
        snapshot = command.snapshots.read(
            ProjectSnapshotRef(snapshot_value.removeprefix("snapshot:"))
        )
        binding = command.catalog.get(started["run_id"])
        assert snapshot.provenance == {
            "schema": "lockstep.run-project-snapshot/v1",
            "public_run_id": started["run_id"],
            "project_identity": str(project.resolve()),
            "definition_digest": binding.recipe_digest,
            "purpose": "run-start",
        }
        assert {entry.path: command.blobs.read(entry.blob) for entry in snapshot.files}[
            "tracked.txt"
        ] == b"exact run-start snapshot bytes\n"
        assert request.required_capabilities == (
            "bounded_result",
            "credentials",
            "network",
            "sandbox",
            "workspace",
        )
        assert request.writes == ("review.md",)
        assert tuple(
            (item.name, item.source_path, item.media_type, item.required)
            for item in request.artifacts
        ) == (("review", "review.md", "text/markdown", True),)
        assert commitment.record.phase == "launching"
        assert commitment.record.launch_commitment_digest is not None
        assert commitment.correlated_prepares == tuple(observer.prepares)

        observer.release()
        released = True
        deadline = time.monotonic() + 10
        managed = command.effects.get(request.effect_id)
        while managed.phase != "delivered" and time.monotonic() < deadline:
            if command._pump_failure is not None:
                raise command._pump_failure
            time.sleep(0.02)
            managed = command.effects.get(request.effect_id)

        assert managed.phase == "delivered"
        assert prepared.adapter.spawn_count == 1
        assert managed.result is not None
        assert managed.result.outcome == "PASS"
        assert managed.result.snapshot_ref is not None
        assert len(managed.result.artifact_refs) == 1
        rollover = command.snapshots.read(
            ProjectSnapshotRef(managed.result.snapshot_ref.removeprefix("snapshot:"))
        )
        rollover_files = {
            entry.path: command.blobs.read(entry.blob) for entry in rollover.files
        }
        artifact = rollover_files["review.md"]
        expected_snapshot = hashlib.sha256()
        for entry in sorted(snapshot.files, key=lambda item: item.path):
            expected_snapshot.update(entry.path.encode("utf-8"))
            expected_snapshot.update(b"\0")
            expected_snapshot.update(command.blobs.read(entry.blob))
            expected_snapshot.update(b"\0")
        expected_snapshot_digest = expected_snapshot.hexdigest()
        assert artifact.startswith(b"# Findings\nControlled evidence-backed review.\n")
        assert f"snapshot_sha256: {expected_snapshot_digest}\n".encode() in artifact
        lines = artifact.decode().splitlines()
        started_ns = int(
            next(line for line in lines if line.startswith("started_ns: ")).split()[1]
        )
        ended_ns = int(
            next(line for line in lines if line.startswith("ended_ns: ")).split()[1]
        )
        assert started_ns < ended_ns
        assert artifact.endswith(b"\n# Verdict\nPASS\n")
    finally:
        if not released:
            observer.release()
        command.close()


def test_reviewed_change_survives_restart_and_publishes_only_with_fresh_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, recipes, owner_state = _public_packaged_reviewed_closure(
        tmp_path, monkeypatch
    )
    first = Engine.command(owner_state, recipes)
    try:
        started = first.start("release", {}, str(project))
        run_id = started["run_id"]
        _wait_for_public_worker_step(
            first, owner_state, recipes, project, run_id, "plan"
        )
        session_id = "reviewed-change-worker"
        assert sessions.touch(owner_state, run_id, session_id, 20) == "bound"

        plan = project / ".lockstep" / "plan.md"
        plan.write_text(
            "# Goal\nShip reviewed code.\n\n"
            "# Acceptance Criteria\nTests pass.\n\n"
            "# Steps\nImplement and review.\n",
            encoding="utf-8",
        )
        assert (
            first.scenario_done(
                run_id,
                "plan",
                {"path": ".lockstep/plan.md"},
                session_id=session_id,
                project=str(project),
            )["status"]
            == "awaiting"
        )

        pytest_marker = tmp_path / "real-pinned-pytest.marker"
        sample_tests = project / "tests"
        sample_tests.mkdir()
        (sample_tests / "test_sample.py").write_text(
            "from pathlib import Path\n\n"
            "def test_real_pinned_pytest_executes():\n"
            f"    Path({str(pytest_marker)!r}).write_text('real pytest\\n')\n",
            encoding="utf-8",
        )
        assert (
            first.scenario_done(
                run_id,
                "tests",
                {"path": "tests/test_sample.py"},
                session_id=session_id,
                project=str(project),
            )["status"]
            == "awaiting"
        )

        sample_src = project / "src"
        sample_src.mkdir()
        (sample_src / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        first.scenario_done(
            run_id,
            "implement",
            {"path": "src/sample.py"},
            session_id=session_id,
            project=str(project),
        )

        binding = first.catalog.get(run_id)
        deadline = time.monotonic() + 20
        managed = None
        pinned = None
        while time.monotonic() < deadline:
            if first._pump_failure is not None:
                raise first._pump_failure
            records = first.effects.list_for_thread(binding.thread_id)
            managed_records = tuple(r for r in records if r.effect_kind == "managed")
            pinned_records = tuple(r for r in records if r.effect_kind == "verify")
            if (
                len(managed_records) == len(pinned_records) == 1
                and managed_records[0].phase == pinned_records[0].phase == "delivered"
            ):
                managed = managed_records[0]
                pinned = pinned_records[0]
                break
            time.sleep(0.02)
        assert managed is not None
        assert pinned is not None
        assert pinned.result is not None and pinned.result.outcome == "PASS"
        assert pytest_marker.read_bytes() == b"real pytest\n"
        assert managed.result is not None
        assert managed.result.outcome == "PASS"
        assert managed.result.snapshot_ref is not None
        assert len(managed.result.artifact_refs) == 1
        artifact_ref = managed.result.artifact_refs[0]
        artifact_record = first.artifacts.read(artifact_ref)
        expected_publication = first.blobs.read(artifact_record.blob)
        assert expected_publication.startswith(
            b"# Findings\nControlled evidence-backed review.\n"
        )
        assert first._runtime_execution_composition.runners.codex.spawn_count == 1
        assert first._runtime_execution_composition.runners.pinned.spawn_count == 1
        assert not (project / ".lockstep" / "review.md").exists()
        with first._admission_recovery_lock:
            first.runtime.bind(binding)
            pending = first.runtime.snapshot(run_id, subgraphs=True)
        assert len(pending.pending) == 1
        descriptor = first._protected_interrupt_descriptor(pending.pending[0])
        assert descriptor is not None
        assert descriptor.kind == "accept"
        accept_step = descriptor.logical_id
    finally:
        first.close()

    reopened = Engine.command(owner_state, recipes)
    try:
        reopened.scenario_recover(str(project), limit=128)
        preview = reopened.preview_publication_consent(
            run_id,
            accept_step,
            project=str(project),
        )
        issued = reopened.issue_publication_consent(
            run_id,
            accept_step,
            preview["digest"],
            project=str(project),
        )
        stored = reopened.authority.inspect_token(issued.token)
        assert stored.commitment.to_dict() == preview
        assert stored.receipt_digest is None

        reopened.scenario_accept_artifact(
            issued.token,
            project=str(project),
        )
        result = _wait_for_public_terminal(reopened, project, run_id)
        redeemed = reopened.authority.inspect_token(issued.token)
        assert redeemed.receipt_digest is not None
        assert result == {
            "status": "completed",
            "run_id": run_id,
            "owner": "engine",
            "next_action": None,
        }
        assert (project / ".lockstep" / "review.md").read_bytes() == (
            expected_publication
        )
        binding = reopened.catalog.get(run_id)
        with reopened._admission_recovery_lock:
            reopened.runtime.bind(binding)
            terminal = reopened.runtime.snapshot(run_id, subgraphs=True)
        assert terminal.pending == ()
        assert terminal.next == ()
        assert reopened._runtime_execution_composition.runners.codex.spawn_count == 0
        acceptance_records = tuple(
            record
            for record in reopened.effects.list_for_thread(binding.thread_id)
            if record.effect_kind in {"accept", "publish"}
        )
        assert tuple(record.phase for record in acceptance_records) == (
            "delivered",
            "delivered",
        )
        accept_result = next(
            record.result
            for record in acceptance_records
            if record.effect_kind == "accept"
        )
        assert accept_result is not None
        assert accept_result.receipt_digest == redeemed.receipt_digest
    finally:
        reopened.close()


def _open_packaged_review_at_pending_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    shared_owner_state: Path | None = None,
    prepared: tuple[Path, Path, Path] | None = None,
):
    project, recipes, owner_state = (
        _public_packaged_reviewed_closure(
            tmp_path,
            monkeypatch,
            shared_owner_state=shared_owner_state,
        )
        if prepared is None
        else prepared
    )
    first = Engine.command(owner_state, recipes)
    try:
        run_id = first.start("release", {}, str(project))["run_id"]
        _wait_for_public_worker_step(
            first, owner_state, recipes, project, run_id, "plan"
        )
        session_id = "adversarial-acceptance-worker"
        assert sessions.touch(owner_state, run_id, session_id, 20) == "bound"

        plan = project / ".lockstep" / "plan.md"
        plan.write_text(
            "# Goal\nFreeze authority.\n\n"
            "# Acceptance Criteria\nExact consent only.\n\n"
            "# Steps\nRun, review, accept.\n",
            encoding="utf-8",
        )
        first.scenario_done(
            run_id,
            "plan",
            {"path": ".lockstep/plan.md"},
            session_id=session_id,
            project=str(project),
        )
        tests = project / "tests"
        tests.mkdir()
        (tests / "test_acceptance_sample.py").write_text(
            "def test_real_pinned_pytest_executes():\n    assert True\n",
            encoding="utf-8",
        )
        first.scenario_done(
            run_id,
            "tests",
            {"path": "tests/test_acceptance_sample.py"},
            session_id=session_id,
            project=str(project),
        )
        source = project / "src"
        source.mkdir()
        (source / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        first.scenario_done(
            run_id,
            "implement",
            {"path": "src/sample.py"},
            session_id=session_id,
            project=str(project),
        )

        binding = first.catalog.get(run_id)
        deadline = time.monotonic() + 20
        managed = None
        while time.monotonic() < deadline:
            if first._pump_failure is not None:
                raise first._pump_failure
            records = first.effects.list_for_thread(binding.thread_id)
            candidates = tuple(
                record for record in records if record.effect_kind == "managed"
            )
            if len(candidates) == 1 and candidates[0].phase == "delivered":
                managed = candidates[0]
                break
            time.sleep(0.02)
        assert managed is not None
        assert managed.result is not None
        assert managed.result.outcome == "PASS"
        assert len(managed.result.artifact_refs) == 1
        artifact = first.artifacts.read(managed.result.artifact_refs[0])
        artifact_bytes = first.blobs.read(artifact.blob)
        assert artifact_bytes.endswith(b"\n# Verdict\nPASS\n")
        with first._admission_recovery_lock:
            first.runtime.bind(binding)
            pending = first.runtime.snapshot(run_id, subgraphs=True)
        accepts = tuple(
            descriptor
            for interrupt in pending.pending
            if (descriptor := first._protected_interrupt_descriptor(interrupt))
            is not None
            and descriptor.kind == "accept"
        )
        assert len(accepts) == 1
        accept_step = accepts[0].logical_id
        assert not (project / ".lockstep" / "review.md").exists()
    finally:
        first.close()

    reopened = Engine.command(owner_state, recipes)
    reopened.scenario_recover(str(project), limit=128)
    reopened.runtime.bind(reopened.catalog.get(run_id))
    recovered = reopened.runtime.snapshot(run_id, subgraphs=True)
    recovered_accepts = tuple(
        descriptor.logical_id
        for interrupt in recovered.pending
        if (descriptor := reopened._protected_interrupt_descriptor(interrupt))
        is not None
        and descriptor.kind == "accept"
    )
    assert recovered_accepts == (accept_step,)
    assert reopened._runtime_execution_composition.runners.codex.spawn_count == 0
    return (
        reopened,
        project,
        recipes,
        owner_state,
        run_id,
        accept_step,
        artifact_bytes,
    )


def _durable_authority_surface(command, project: Path, run_id: str, owner_state: Path):
    def rows(table, *, where=None):
        statement = table.select()
        if where is not None:
            statement = statement.where(where)
        with command.store.read_connection() as connection:
            selected = connection.execute(statement).mappings()
            return tuple(
                sorted(
                    (tuple(sorted(dict(row).items())) for row in selected),
                    key=repr,
                )
            )

    effects = command.store.tables.effects
    publication_root = owner_state / "publications"
    publication_files = ()
    if publication_root.exists():
        publication_files = tuple(
            (path.relative_to(publication_root).as_posix(), path.read_bytes())
            for path in sorted(publication_root.rglob("*"))
            if path.is_file()
        )
    destination = project / ".lockstep" / "review.md"
    return {
        "consents": rows(command.store.tables.publication_consents),
        "epochs": rows(command.store.tables.consent_epochs),
        "accept_publish_effects": rows(
            effects,
            where=effects.c.effect_kind.in_(("accept", "publish")),
        ),
        "native": command.runtime.snapshot(run_id, subgraphs=True),
        "publication_files": publication_files,
        "destination": destination.read_bytes() if destination.exists() else None,
    }


def _commitment_from_public_preview(
    preview: dict[str, object],
) -> PublicationConsentCommitment:
    source = preview["source"]
    assert isinstance(source, dict)
    return PublicationConsentCommitment(
        schema="lockstep.publication-consent-commitment/v1",
        public_run_id=str(preview["public_run_id"]),
        project_identity=str(preview["project_identity"]),
        definition_digest=str(preview["definition_digest"]),
        source=NativeCoordinate(
            str(source["thread_id"]),
            str(source["checkpoint_id"]),
            str(source["checkpoint_ns"]),
            str(source["task_id"]),
            str(source["interrupt_id"]),
        ),
        effect_id=str(preview["effect_id"]),
        descriptor_digest=str(preview["descriptor_digest"]),
        producer_effect_id=str(preview["producer_effect_id"]),
        artifact_ref=str(preview["artifact_ref"]),
        artifact_digest=str(preview["artifact_digest"]),
        destination=str(preview["destination"]),
        transformation="identity",
        audience="local-project",
        digest=str(preview["digest"]),
    )


def _rehash_commitment(
    commitment: PublicationConsentCommitment,
) -> PublicationConsentCommitment:
    values = commitment.to_dict()
    values.pop("digest")
    digest = hashlib.sha256(
        json.dumps(
            values,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return replace(commitment, digest=digest)


def _commitment_with_wrong_facet(
    commitment: PublicationConsentCommitment,
    facet: str,
) -> PublicationConsentCommitment:
    if facet == "coordinate":
        changes: dict[str, object] = {
            "source": replace(commitment.source, checkpoint_id="foreign-checkpoint")
        }
    else:
        changes = {
            "foreign_bearer": {"effect_id": "foreign-accept-effect"},
            "artifact_ref": {"artifact_ref": "artifact:" + "0" * 64},
            "artifact_digest": {"artifact_digest": "0" * 64},
            "destination": {"destination": ".lockstep/other-review.md"},
            "definition": {"definition_digest": "0" * 64},
            "run": {"public_run_id": "foreign-run"},
            "project": {"project_identity": commitment.project_identity + "-foreign"},
            "producer": {"producer_effect_id": "foreign-producer-effect"},
            "descriptor": {"descriptor_digest": "0" * 64},
            "transformation": {"transformation": "rewrite"},
            "audience": {"audience": "external"},
        }[facet]
    changed = replace(commitment, **changes)  # type: ignore[arg-type]
    return _rehash_commitment(changed)


def test_public_acceptance_rejects_every_wrong_commitment_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        command,
        project,
        recipes,
        owner_state,
        run_id,
        accept_step,
        artifact_bytes,
    ) = _open_packaged_review_at_pending_acceptance(tmp_path, monkeypatch)
    try:
        assert artifact_bytes.endswith(b"\n# Verdict\nPASS\n")
        initial = _durable_authority_surface(command, project, run_id, owner_state)
        assert initial["consents"] == ()
        assert initial["publication_files"] == ()
        assert all(
            ("effect_kind", "publish") not in row
            for row in initial["accept_publish_effects"]
        )
        assert initial["destination"] is None

        for absent_or_foreign in ("", "foreign-bearer-with-no-durable-consent"):
            before = _durable_authority_surface(command, project, run_id, owner_state)
            with pytest.raises(LockstepError, match="invalid|stale"):
                command.scenario_accept_artifact(
                    absent_or_foreign, project=str(project)
                )
            assert (
                _durable_authority_surface(command, project, run_id, owner_state)
                == before
            )

        preview = command.preview_publication_consent(
            run_id, accept_step, project=str(project)
        )
        exact = _commitment_from_public_preview(preview)
        assert exact.to_dict() == preview
        registered = command.artifacts.read(exact.artifact_ref)
        assert command.blobs.read(registered.blob) == artifact_bytes
        assert registered.blob.sha256 == exact.artifact_digest
        assert registered.public_run_id == exact.public_run_id
        assert registered.project_identity == exact.project_identity
        assert registered.definition_digest == exact.definition_digest
        assert registered.producer_effect_id == exact.producer_effect_id
        producer = command.effects.get(exact.producer_effect_id)
        assert producer.coordinate == registered.producer_coordinate
        assert producer.descriptor_digest == registered.descriptor_digest
        assert registered.descriptor_digest != exact.descriptor_digest
        facets = (
            "foreign_bearer",
            "artifact_ref",
            "artifact_digest",
            "destination",
            "definition",
            "run",
            "project",
            "coordinate",
            "producer",
            "descriptor",
            "transformation",
            "audience",
        )
        for facet in facets:
            attacker = command.authority.issue(
                _commitment_with_wrong_facet(exact, facet)
            )
            before = _durable_authority_surface(command, project, run_id, owner_state)
            with pytest.raises(
                LockstepError, match="invalid|stale|not found|unknown run"
            ):
                command.scenario_accept_artifact(attacker.token, project=str(project))
            assert (
                _durable_authority_surface(command, project, run_id, owner_state)
                == before
            )

        delivered_retry_attacker = command.authority.issue(
            _commitment_with_wrong_facet(
                _commitment_with_wrong_facet(exact, "coordinate"), "audience"
            )
        )

        issued = command.issue_publication_consent(
            run_id, accept_step, preview["digest"], project=str(project)
        )
        command.scenario_accept_artifact(issued.token, project=str(project))
        completed = _wait_for_public_terminal(command, project, run_id)
        assert (project / ".lockstep" / "review.md").read_bytes() == artifact_bytes
        command.close()
        command = Engine.command(owner_state, recipes)
        command.scenario_recover(str(project), limit=128)
        command.runtime.bind(command.catalog.get(run_id))
        with command._admission_recovery_lock:
            after_exact = _durable_authority_surface(
                command, project, run_id, owner_state
            )
            with pytest.raises(LockstepError, match="invalid|stale"):
                command.scenario_accept_artifact(
                    delivered_retry_attacker.token, project=str(project)
                )
            assert (
                _durable_authority_surface(command, project, run_id, owner_state)
                == after_exact
            )
            assert (
                command.scenario_accept_artifact(issued.token, project=str(project))
                == completed
            )
            assert (
                _durable_authority_surface(command, project, run_id, owner_state)
                == after_exact
            )
    finally:
        command.close()


def test_public_acceptance_rejects_stale_revoked_bearer_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        command,
        project,
        recipes,
        owner_state,
        run_id,
        accept_step,
        _artifact_bytes,
    ) = _open_packaged_review_at_pending_acceptance(tmp_path, monkeypatch)
    try:
        preview = command.preview_publication_consent(
            run_id, accept_step, project=str(project)
        )
        issued = command.issue_publication_consent(
            run_id, accept_step, preview["digest"], project=str(project)
        )
        assert command.revoke_publication_consents(project=str(project)) == 2
    finally:
        command.close()

    restarted = Engine.command(owner_state, recipes)
    try:
        restarted.scenario_recover(str(project), limit=128)
        restarted.runtime.bind(restarted.catalog.get(run_id))
        before = _durable_authority_surface(restarted, project, run_id, owner_state)
        with pytest.raises(LockstepError, match="invalid|stale"):
            restarted.scenario_accept_artifact(issued.token, project=str(project))
        assert (
            _durable_authority_surface(restarted, project, run_id, owner_state)
            == before
        )
        assert (project / ".lockstep" / "review.md").exists() is False
    finally:
        restarted.close()


def test_public_bearers_are_bound_to_one_complete_cross_project_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_root = tmp_path / "original"
    foreign_root = tmp_path / "foreign"
    original_root.mkdir()
    foreign_root.mkdir()
    (
        original_project,
        original_recipes,
        original_owner_state,
        original_index,
    ) = _installed_packaged_reviewed_closure(original_root, monkeypatch)
    (
        foreign_project,
        foreign_recipes,
        foreign_owner_state,
        foreign_index,
    ) = _installed_packaged_reviewed_closure(
        foreign_root,
        monkeypatch,
        shared_owner_state=original_owner_state,
    )
    inventory = RuntimeProvisioningInventory.combine((original_index, foreign_index))
    _provision_reviewed_inventory(
        original_root / "shared-reviewed-runtime",
        owner_state=original_owner_state,
        index=inventory,
        project=original_project,
    )
    original = _open_packaged_review_at_pending_acceptance(
        original_root,
        monkeypatch,
        prepared=(original_project, original_recipes, original_owner_state),
    )
    (
        original_command,
        original_project,
        _original_recipes,
        original_owner_state,
        original_run_id,
        original_step,
        original_artifact_bytes,
    ) = original
    original_command.close()
    foreign = _open_packaged_review_at_pending_acceptance(
        foreign_root,
        monkeypatch,
        prepared=(foreign_project, foreign_recipes, foreign_owner_state),
    )
    (
        foreign_command,
        foreign_project,
        _foreign_recipes,
        foreign_owner_state,
        foreign_run_id,
        foreign_step,
        foreign_artifact_bytes,
    ) = foreign
    original_command = Engine.command(original_owner_state, _original_recipes)
    original_command.scenario_recover(str(original_project), limit=128)
    original_command.runtime.bind(original_command.catalog.get(original_run_id))
    assert foreign_owner_state == original_owner_state
    try:
        original_preview = original_command.preview_publication_consent(
            original_run_id, original_step, project=str(original_project)
        )
        foreign_preview = foreign_command.preview_publication_consent(
            foreign_run_id, foreign_step, project=str(foreign_project)
        )
        original_issued = original_command.issue_publication_consent(
            original_run_id,
            original_step,
            original_preview["digest"],
            project=str(original_project),
        )
        foreign_issued = foreign_command.issue_publication_consent(
            foreign_run_id,
            foreign_step,
            foreign_preview["digest"],
            project=str(foreign_project),
        )
        assert (
            original_command.authority.inspect_token(
                original_issued.token
            ).commitment.to_dict()
            == original_preview
        )
        assert (
            original_command.authority.inspect_token(
                foreign_issued.token
            ).commitment.to_dict()
            == foreign_preview
        )
        assert (
            foreign_command.authority.inspect_token(
                original_issued.token
            ).commitment.to_dict()
            == original_preview
        )
        assert (
            foreign_command.authority.inspect_token(
                foreign_issued.token
            ).commitment.to_dict()
            == foreign_preview
        )
        assert (
            original_preview["project_identity"] != foreign_preview["project_identity"]
        )
        assert original_preview["public_run_id"] != foreign_preview["public_run_id"]
        assert original_preview["artifact_ref"] != foreign_preview["artifact_ref"]
        assert original_preview["digest"] != foreign_preview["digest"]
        assert original_issued.token != foreign_issued.token

        original_before = _durable_authority_surface(
            original_command,
            original_project,
            original_run_id,
            original_owner_state,
        )
        with pytest.raises(LockstepError, match="invalid|stale"):
            original_command.scenario_accept_artifact(
                foreign_issued.token, project=str(original_project)
            )
        assert (
            _durable_authority_surface(
                original_command,
                original_project,
                original_run_id,
                original_owner_state,
            )
            == original_before
        )
        assert (
            foreign_command.authority.inspect_token(foreign_issued.token).receipt_digest
            is None
        )

        foreign_before = _durable_authority_surface(
            foreign_command,
            foreign_project,
            foreign_run_id,
            foreign_owner_state,
        )
        with pytest.raises(LockstepError, match="invalid|stale"):
            foreign_command.scenario_accept_artifact(
                original_issued.token, project=str(foreign_project)
            )
        assert (
            _durable_authority_surface(
                foreign_command,
                foreign_project,
                foreign_run_id,
                foreign_owner_state,
            )
            == foreign_before
        )
        assert (
            original_command.authority.inspect_token(
                original_issued.token
            ).receipt_digest
            is None
        )

        original_command.scenario_accept_artifact(
            original_issued.token, project=str(original_project)
        )
        assert (
            _wait_for_public_terminal(
                original_command, original_project, original_run_id
            )["status"]
            == "completed"
        )
        foreign_command.scenario_accept_artifact(
            foreign_issued.token, project=str(foreign_project)
        )
        assert (
            _wait_for_public_terminal(foreign_command, foreign_project, foreign_run_id)[
                "status"
            ]
            == "completed"
        )
        assert (original_project / ".lockstep" / "review.md").read_bytes() == (
            original_artifact_bytes
        )
        assert (foreign_project / ".lockstep" / "review.md").read_bytes() == (
            foreign_artifact_bytes
        )
        original_stored = original_command.authority.inspect_token(
            original_issued.token
        )
        foreign_stored = foreign_command.authority.inspect_token(foreign_issued.token)
        assert original_stored.commitment.to_dict() == original_preview
        assert foreign_stored.commitment.to_dict() == foreign_preview
        assert original_stored.consent_ref != foreign_stored.consent_ref
        assert original_stored.receipt_digest is not None
        assert foreign_stored.receipt_digest is not None
        assert original_stored.receipt_digest != foreign_stored.receipt_digest
    finally:
        original_command.close()
        foreign_command.close()


def test_real_codex_managed_vertical_uses_disposable_git_project(
    tmp_path: Path,
) -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex CLI is not installed")
    version = subprocess.run(
        [codex, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    codex_home_raw = os.environ.get("LOCKSTEP_CODEX_SMOKE_HOME")
    model = os.environ.get("LOCKSTEP_CODEX_SMOKE_MODEL")
    if codex_home_raw is None or model is None:
        pytest.skip("dedicated Codex smoke credentials/model are unavailable")
    codex_home = Path(codex_home_raw)
    if not (codex_home / "auth.json").is_file():
        pytest.skip("dedicated Codex smoke credential is unavailable")

    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    private_tmp = owner / "tmp"
    private_tmp.mkdir(mode=0o700)
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    seed = snapshots.capture(
        {"src/README.md": blobs.put(b"managed smoke input\n")},
        declared_paths=("src/",),
        provenance={"source": "real-codex-smoke"},
    )
    binding = CodexInstallationBinding.capture(
        executable=codex,
        model=model,
        cli_version=version,
        permission_profile={"sandbox": "workspace-write", "approval": "never"},
        codex_home=codex_home,
        environment={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(private_tmp),
        },
    )
    workspaces = LocalGitWorkspaceProvider(owner, snapshots, blobs)
    adapter = CodexRunnerAdapter(
        owner_state_dir=owner,
        installation=lambda: binding,
        decision_gate=CodexLaunchDecisionGate(binding.digest, generation=1),
        workspaces=workspaces,
        blobs=blobs,
        sandbox=CodexSandboxAttestor(cli_version=version),
    )
    coordinate = NativeCoordinate("thread", "checkpoint", "", "task", "interrupt")
    intent = EffectRequest.build(
        effect_id="real-codex-smoke",
        public_run_id="run-real-codex-smoke",
        project_identity="disposable-project",
        definition_digest="a" * 64,
        coordinate=coordinate,
        descriptor_digest="b" * 64,
        effect_kind="managed",
        runner_selector="codex",
        runner_binding_digest=adapter.binding_digest,
        required_capabilities=(
            "workspace",
            "bounded_result",
            "sandbox",
            "network",
            "credentials",
        ),
        inputs=(
            (
                "brief",
                (
                    "Create src/smoke.txt containing exactly the text "
                    "'lockstep managed smoke' followed by one newline. "
                    "Do not modify any other project file."
                ),
            ),
            ("snapshot", f"snapshot:{seed.digest}"),
        ),
        writes=("src/",),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    grant = EffectGrant.build(
        intent,
        actor_binding_digest="c" * 64,
        required_authorities=("os_user_execution",),
        workspace_ref=workspaces.workspace_ref_for(
            intent.effect_id, intent.intent_digest
        ),
        parent_capability_generation=1,
        grant_generation=1,
        policy_epoch=1,
        config_epoch=1,
        approval_generation=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    request = intent.bind_grant(grant)

    launch = adapter.prepare(request)
    assert launch.workspace_ref is not None
    workspace = workspaces.inspect(launch.workspace_ref)
    assert workspace.workspace_path.is_relative_to(tmp_path)
    assert (workspace.workspace_path / ".git").is_dir()
    adapter.ensure_started(launch)
    terminal = adapter.wait_terminal(request.effect_id, timeout=300)

    assert terminal.state == "terminal"
    assert terminal.result is not None
    assert terminal.result.outcome == "PASS"
    assert terminal.result.snapshot_ref is not None
    safety = adapter.quiesce(request.effect_id)
    assert safety.state == "proven"
    assert safety.rollover_snapshot_ref == terminal.result.snapshot_ref
    successor = snapshots.read(
        ProjectSnapshotRef(terminal.result.snapshot_ref.removeprefix("snapshot:"))
    )
    files = {entry.path: blobs.read(entry.blob) for entry in successor.files}
    assert files["src/smoke.txt"] == b"lockstep managed smoke\n"
    assert workspaces.inspect(launch.workspace_ref).phase == "released"
