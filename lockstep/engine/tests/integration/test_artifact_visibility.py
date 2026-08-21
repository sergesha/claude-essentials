from __future__ import annotations

from pathlib import Path

import yaml

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRegistry
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.effects.descriptors import derive_effect_id, parse_effect_descriptor
from lockstep.runtime.project_snapshots import ProjectSnapshotStore


def test_registered_artifact_is_invisible_until_exact_native_resume_after_restart(
    tmp_path: Path,
) -> None:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "managed",
        "logical_id": "review",
        "runner": {
            "selector": "codex",
            "required_capabilities": ["workspace", "bounded_result"],
        },
        "inputs": {},
        "writes": ["review.md"],
        "artifacts": [
            {
                "name": "review",
                "source_path": "review.md",
                "media_type": "text/markdown",
                "required": True,
            }
        ],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    recipe = tmp_path / "artifact.recipe.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "name": "artifact",
                "state": {"effect_result": "dict", "lockstep_outcome": "str"},
                "nodes": {
                    "effect": {
                        "type": "interrupt",
                        "state_key": "effect_request",
                        "resume_key": "effect_result",
                        "idempotent": False,
                        "message": {"lockstep_effect": descriptor},
                    },
                    "done": {
                        "type": "passthrough",
                        "output": {"lockstep_outcome": "PASS"},
                    },
                },
                "edges": [
                    {"from": "START", "to": "effect"},
                    {"from": "effect", "to": "done"},
                    {"from": "done", "to": "END"},
                ],
            },
            sort_keys=False,
        )
    )
    database = tmp_path / "native.sqlite"
    first = yg._open_native_path(recipe, database)  # noqa: SLF001 - restart oracle
    parked = first.invoke({}, thread_id="artifact-thread")
    coordinate = parked.pending[0].coordinate
    parsed = parse_effect_descriptor(descriptor)
    effect_id = derive_effect_id(coordinate, parsed.digest)

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    rollover = snapshots.capture(
        {"review.md": blobs.put(b"review\n")},
        declared_paths=("review.md",),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": "f" * 64,
            "workspace_ref": "workspace:one",
        },
    )
    registry = ArtifactRegistry(owner, blobs, snapshots)
    registration = dict(
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="d" * 64,
        producer_effect_id=effect_id,
        producer_request_digest="f" * 64,
        workspace_ref="workspace:one",
        producer_coordinate=coordinate,
        descriptor_digest=parsed.digest,
        snapshot_ref=rollover,
        declarations=(
            ArtifactDeclaration("review", "review.md", "text/markdown", True),
        ),
    )
    refs = registry.register_set(**registration)

    assert "effect_result" not in parked.values
    first.close()
    second = yg._open_native_path(recipe, database)  # noqa: SLF001 - restart oracle
    restarted = second.snapshot(thread_id="artifact-thread", subgraphs=True)
    assert "effect_result" not in restarted.values
    assert registry.register_set(**registration) == refs

    completed = second.resume(
        thread_id="artifact-thread",
        results_by_interrupt_id={
            coordinate.interrupt_id: {
                "schema": "lockstep.effect-result/v1",
                "effect_id": effect_id,
                "outcome": "PASS",
                "result_ref": "blob:" + "a" * 64,
                "artifact_refs": [str(refs[0])],
                "snapshot_ref": f"snapshot:{rollover.digest}",
                "diff_ref": None,
                "fixed_error_code": None,
                "evidence_refs": [],
            }
        },
    )
    second.close()

    assert completed.values["effect_result"]["artifact_refs"] == [str(refs[0])]
    assert completed.values["lockstep_outcome"] == "PASS"
