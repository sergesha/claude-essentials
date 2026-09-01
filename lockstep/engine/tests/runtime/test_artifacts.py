from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotStore


def _coordinate(*, checkpoint: str = "cp-1") -> NativeCoordinate:
    return NativeCoordinate(
        thread_id="thread-1",
        checkpoint_ns="child:",
        checkpoint_id=checkpoint,
        task_id="task-1",
        interrupt_id="interrupt-1",
    )


def _stores(tmp_path: Path):
    from lockstep.runtime.artifacts import ArtifactRegistry

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    return blobs, snapshots, ArtifactRegistry(owner, blobs, snapshots)


def _snapshot(blobs, snapshots, files: dict[str, bytes]):
    refs = {path: blobs.put(content) for path, content in files.items()}
    return snapshots.capture(
        refs,
        declared_paths=tuple(refs),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": "f" * 64,
            "workspace_ref": "workspace:one",
        },
    )


def test_registry_admits_exact_snapshot_bytes_and_immutable_provenance(tmp_path: Path) -> None:
    from lockstep.runtime.artifacts import ArtifactDeclaration

    blobs, snapshots, registry = _stores(tmp_path)
    snapshot = _snapshot(blobs, snapshots, {"review.md": b"Verdict: PASS\n"})
    declaration = ArtifactDeclaration(
        name="review",
        source_path="review.md",
        media_type="text/markdown",
        required=True,
    )

    refs = registry.register_set(
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="d" * 64,
        producer_effect_id="effect-1",
        producer_request_digest="f" * 64,
        workspace_ref="workspace:one",
        producer_coordinate=_coordinate(),
        descriptor_digest="a" * 64,
        snapshot_ref=snapshot,
        declarations=(declaration,),
    )

    assert len(refs) == 1
    record = registry.read(refs[0])
    assert record.declared_name == "review"
    assert record.source_path == "review.md"
    assert record.source_snapshot_ref == snapshot
    assert record.producer_coordinate == _coordinate()
    assert record.descriptor_digest == "a" * 64
    assert record.definition_digest == "d" * 64
    assert record.producer_request_digest == "f" * 64
    assert record.workspace_ref == "workspace:one"
    assert record.blob.sha256 == hashlib.sha256(b"Verdict: PASS\n").hexdigest()
    assert blobs.read(record.blob) == b"Verdict: PASS\n"


def test_registry_key_is_exact_coordinate_and_declared_name(tmp_path: Path) -> None:
    from lockstep.runtime.artifacts import ArtifactCollision, ArtifactDeclaration

    blobs, snapshots, registry = _stores(tmp_path)
    first_snapshot = _snapshot(blobs, snapshots, {"review.md": b"one"})
    declaration = ArtifactDeclaration("review", "review.md", "text/markdown", True)
    kwargs = dict(
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="d" * 64,
        producer_effect_id="effect-1",
        producer_request_digest="f" * 64,
        workspace_ref="workspace:one",
        producer_coordinate=_coordinate(),
        descriptor_digest="a" * 64,
        snapshot_ref=first_snapshot,
        declarations=(declaration,),
    )
    first = registry.register_set(**kwargs)
    assert registry.register_set(**kwargs) == first

    changed = _snapshot(blobs, snapshots, {"review.md": b"two"})
    with pytest.raises(ArtifactCollision):
        registry.register_set(**{**kwargs, "snapshot_ref": changed})

    other = registry.register_set(
        **{**kwargs, "producer_coordinate": _coordinate(checkpoint="cp-2"), "snapshot_ref": changed}
    )
    assert other != first


def test_registry_validates_complete_set_before_publishing_any_manifest(tmp_path: Path) -> None:
    from lockstep.runtime.artifacts import ArtifactDeclaration, MissingArtifact

    blobs, snapshots, registry = _stores(tmp_path)
    snapshot = _snapshot(blobs, snapshots, {"present.md": b"present"})
    declarations = (
        ArtifactDeclaration("present", "present.md", "text/markdown", True),
        ArtifactDeclaration("missing", "missing.md", "text/markdown", True),
    )

    with pytest.raises(MissingArtifact):
        registry.register_set(
            public_run_id="run-1",
            project_identity="project-1",
            definition_digest="d" * 64,
            producer_effect_id="effect-1",
            producer_request_digest="f" * 64,
            workspace_ref="workspace:one",
            producer_coordinate=_coordinate(),
            descriptor_digest="a" * 64,
            snapshot_ref=snapshot,
            declarations=declarations,
        )

    assert registry.list_for_producer(
        "effect-1", _coordinate(), "a" * 64, ("present", "missing")
    ) == ()


def test_registry_rejects_foreign_snapshot_provenance_and_unsafe_sources(tmp_path: Path) -> None:
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactProvenanceError

    blobs, snapshots, registry = _stores(tmp_path)
    snapshot = _snapshot(blobs, snapshots, {"review.md": b"ok"})
    for source in ("../review.md", ".git/config"):
        with pytest.raises((ValueError, ArtifactProvenanceError)):
            declaration = ArtifactDeclaration(
                "review", source, "text/markdown", True
            )
            registry.register_set(
                public_run_id="run-1",
                project_identity="project-1",
                definition_digest="d" * 64,
                producer_effect_id="effect-1",
                producer_request_digest="f" * 64,
                workspace_ref="workspace:one",
                producer_coordinate=_coordinate(),
                descriptor_digest="a" * 64,
                snapshot_ref=snapshot,
                declarations=(declaration,),
            )

    foreign = snapshots.capture(
        {"review.md": blobs.put(b"foreign")},
        declared_paths=("review.md",),
        provenance={"source": "untrusted-import"},
    )
    with pytest.raises(ArtifactProvenanceError):
        registry.register_set(
            public_run_id="run-1",
            project_identity="project-1",
            definition_digest="d" * 64,
            producer_effect_id="other-effect",
            producer_request_digest="f" * 64,
            workspace_ref="workspace:one",
            producer_coordinate=_coordinate(),
            descriptor_digest="a" * 64,
            snapshot_ref=foreign,
            declarations=(ArtifactDeclaration("review", "review.md", "text/markdown", True),),
        )


def test_registry_enforces_hard_cardinality_and_manifest_limits(tmp_path: Path) -> None:
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactLimits, ArtifactRegistry
    from lockstep.runtime.owner_state import StorageLimitExceeded

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    snapshot = _snapshot(blobs, snapshots, {"a": b"a", "b": b"b"})
    registry = ArtifactRegistry(
        owner, blobs, snapshots, limits=ArtifactLimits(max_artifacts_per_set=1)
    )

    with pytest.raises(StorageLimitExceeded):
        registry.register_set(
            public_run_id="run-1",
            project_identity="project-1",
            definition_digest="d" * 64,
            producer_effect_id="effect-1",
            producer_request_digest="f" * 64,
            workspace_ref="workspace:one",
            producer_coordinate=_coordinate(),
            descriptor_digest="a" * 64,
            snapshot_ref=snapshot,
            declarations=(
                ArtifactDeclaration("a", "a", "text/plain", True),
                ArtifactDeclaration("b", "b", "text/plain", True),
            ),
        )


def test_registry_crash_before_set_commit_exposes_no_partial_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRef

    blobs, snapshots, registry = _stores(tmp_path)
    snapshot = _snapshot(blobs, snapshots, {"one": b"ONE", "two": b"TWO"})
    declarations = (
        ArtifactDeclaration("one", "one", "text/plain", True),
        ArtifactDeclaration("two", "two", "text/plain", True),
    )
    original = registry._publish_immutable
    published = 0

    def crash(path, encoded, *, collision):
        nonlocal published
        original(path, encoded, collision=collision)
        published += 1
        if published == 2:
            raise RuntimeError("crash before producer-set commit")

    monkeypatch.setattr(registry, "_publish_immutable", crash)
    with pytest.raises(RuntimeError, match="producer-set commit"):
        registry.register_set(
            public_run_id="run-1",
            project_identity="project-1",
            definition_digest="d" * 64,
            producer_effect_id="effect-1",
            producer_request_digest="f" * 64,
            workspace_ref="workspace:one",
            producer_coordinate=_coordinate(),
            descriptor_digest="a" * 64,
            snapshot_ref=snapshot,
            declarations=declarations,
        )

    assert registry.list_for_producer(
        "effect-1", _coordinate(), "a" * 64, ("one", "two")
    ) == ()
    manifests = tuple((tmp_path / "owner/artifacts/manifests").glob("*.json"))
    assert manifests
    with pytest.raises(KeyError):
        registry.read(ArtifactRef(manifests[0].stem))
