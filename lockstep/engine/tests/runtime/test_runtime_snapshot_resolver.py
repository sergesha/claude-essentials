from __future__ import annotations

from pathlib import Path

from lockstep.runtime.effects.authority import EffectAuthorityDenied
from lockstep.runtime.engine import Engine
from lockstep.runtime.project_snapshots import ProjectSnapshotRef
from lockstep.runtime.service import LockstepCommandService
from lockstep.workflow.compiler import compile_workflow
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import ResolvedCatalog, validate_semantics

from tests.runtime.providers.fakes import (
    FakeEffectAuthority,
    FakeRunner,
    _legacy_command_service,
)


class _AutoGrantAuthority(FakeEffectAuthority):
    def resolve(self, intent):
        try:
            return super().resolve(intent)
        except EffectAuthorityDenied:
            self.authorize(intent)
            return super().resolve(intent)


def _compile(tmp_path: Path, name: str, flow: str):
    source = tmp_path / f"{name}.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\n"
        f"name: {name}\n"
        "description: runtime snapshot contract\n"
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


def test_runtime_snapshot_input_is_durable_and_reused_after_restart(
    tmp_path: Path,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "verify-project",
        "  - verify:\n"
        "      id: tests\n"
        "      command: pytest -q\n"
        "      timeout: 60\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "tracked.txt").write_text("start bytes\n")
    state = tmp_path / "state"
    runner = FakeRunner()
    authority = _AutoGrantAuthority()

    first = _legacy_command_service(
        state, recipes, runners={"pinned": runner}, effect_authority=authority
    )
    started = first.start(
        "verify-project",
        {},
        str(project),
        compiler_provenance=compiled.compiler_provenance,
    )
    assert runner.prepare_calls
    request = runner.prepare_calls[0]
    snapshot_value = dict(request.inputs)["snapshot"]
    assert snapshot_value.startswith("snapshot:")
    snapshot_ref = ProjectSnapshotRef(snapshot_value.removeprefix("snapshot:"))
    captured = first.snapshots.read(snapshot_ref)
    assert captured.provenance == {
        "schema": "lockstep.run-project-snapshot/v1",
        "public_run_id": started["run_id"],
        "project_identity": str(project.resolve()),
        "definition_digest": first.catalog.get(started["run_id"]).recipe_digest,
        "purpose": "run-start",
    }
    (project / "tracked.txt").write_text("mutated after durable bind\n")
    first.close()

    restarted = _legacy_command_service(
        state, recipes, runners={"pinned": runner}, effect_authority=authority
    )
    Engine.observe(state, recipes).status(started["run_id"], str(project))
    restarted.close()

    assert all(
        dict(observed.inputs)["snapshot"] == snapshot_value
        for observed in runner.prepare_calls
    )


def test_decision_descriptor_executes_without_a_runner_from_exact_snapshots(
    tmp_path: Path,
) -> None:
    recipes, compiled = _compile(
        tmp_path,
        "route",
        "  - decide:\n"
        "      id: risk\n"
        "      using:\n"
        "        type: changed-paths\n"
        "        since: start\n"
        "        cases: {high: [auth/**]}\n"
        "        default: low\n"
        "  - choose:\n"
        "      value: risk\n"
        "      cases:\n"
        "        high: [{escalate: {}}]\n"
        "        low: [{escalate: {}}]\n",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("unchanged\n")
    service = LockstepCommandService(tmp_path / "state", recipes)
    try:
        result = service.start(
            "route",
            {},
            str(project),
            compiler_provenance=compiled.compiler_provenance,
        )
        snapshot = service.runtime.snapshot(result["run_id"], subgraphs=True)
    finally:
        service.close()

    assert result["status"] == "escalated"
    assert snapshot.values["risk_result"] == {
        "schema": "lockstep.decision-result/v1",
        "effect_id": snapshot.values["risk_result"]["effect_id"],
        "outcome": "PASS",
        "decision_digest": snapshot.values["risk_result"]["decision_digest"],
        "value": "low",
    }


def _stored_snapshot(store, blobs, marker: str, *, previous=None, provenance=None):
    blob = blobs.put(marker.encode())
    return store.capture(
        {f"{marker}.txt": blob},
        declared_paths=(f"{marker}.txt",),
        provenance=provenance or {"schema": "test.snapshot/v1"},
        previous=previous,
    )


def test_divergent_fan_in_uses_snapshot_chain_common_ancestor_not_global_latest(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore
    from lockstep.runtime.snapshot_resolver import resolve_lineage_snapshot

    blobs = BlobStore(tmp_path / "owner")
    snapshots = ProjectSnapshotStore(tmp_path / "owner", blobs)
    start = _stored_snapshot(snapshots, blobs, "start")
    left = _stored_snapshot(snapshots, blobs, "left", previous=start)
    right = _stored_snapshot(snapshots, blobs, "right", previous=start)

    assert resolve_lineage_snapshot((left, right), snapshots) == start
    assert resolve_lineage_snapshot((left,), snapshots) == left


def test_foreign_or_missing_runtime_snapshot_fails_before_authority_ports(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.catalog import RunBinding
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore
    from lockstep.runtime.snapshot_resolver import verify_bound_snapshot

    blobs = BlobStore(tmp_path / "owner")
    snapshots = ProjectSnapshotStore(tmp_path / "owner", blobs)
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64,
        str((tmp_path / "project").resolve()),
    )
    foreign = _stored_snapshot(
        snapshots,
        blobs,
        "foreign",
        provenance={
            "schema": "lockstep.run-project-snapshot/v1",
            "public_run_id": "run-foreign",
            "project_identity": binding.project_identity,
            "definition_digest": binding.recipe_digest,
            "purpose": "run-start",
        },
    )

    for ref in (foreign, ProjectSnapshotRef("f" * 64)):
        try:
            verify_bound_snapshot(ref, snapshots, binding)
        except Exception:
            pass
        else:
            raise AssertionError("foreign/missing snapshot was accepted")


def test_manual_or_publication_snapshot_is_sealed_as_a_chain_successor(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.blobs import BlobStore
    from lockstep.runtime.catalog import RunBinding
    from lockstep.runtime.project_snapshots import ProjectSnapshotStore
    from lockstep.runtime.snapshot_resolver import capture_authoritative_snapshot

    project = tmp_path / "project"
    project.mkdir()
    (project / "result.txt").write_text("before\n")
    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64,
        str(project.resolve()),
    )
    start = capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=None, purpose="run-start"
    )
    (project / "result.txt").write_text("accepted publication\n")
    published = capture_authoritative_snapshot(
        project, snapshots, blobs, binding, previous=start, purpose="publication"
    )

    assert snapshots.read(published).previous == start
    assert snapshots.read(published).provenance["purpose"] == "publication"
