from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.project_snapshots import ProjectSnapshotStore


def _coordinate() -> NativeCoordinate:
    return NativeCoordinate("thread-1", "child:", "cp-1", "task-1", "interrupt-1")


def _registry(tmp_path: Path, files: dict[str, bytes]):
    from lockstep.runtime.artifacts import ArtifactDeclaration, ArtifactRegistry

    owner = tmp_path / "owner"
    blobs = BlobStore(owner)
    snapshots = ProjectSnapshotStore(owner, blobs)
    snapshot = snapshots.capture(
        {path: blobs.put(content) for path, content in files.items()},
        declared_paths=tuple(files),
        provenance={
            "source": "managed-workspace-rollover",
            "request_digest": "f" * 64,
            "workspace_ref": "workspace:one",
        },
    )
    registry = ArtifactRegistry(owner, blobs, snapshots)
    refs = registry.register_set(
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="d" * 64,
        producer_effect_id="producer",
        producer_request_digest="f" * 64,
        workspace_ref="workspace:one",
        producer_coordinate=_coordinate(),
        descriptor_digest="a" * 64,
        snapshot_ref=snapshot,
        declarations=tuple(
            ArtifactDeclaration(path, path, "text/plain", True) for path in files
        ),
    )
    return owner, blobs, registry, refs


def _request(refs, destinations, *, publisher_binding_digest: str):
    from lockstep.runtime.publication import PublicationEntry, PublicationRequest

    return PublicationRequest.build(
        effect_id="publish-1",
        public_run_id="run-1",
        project_identity="project-1",
        definition_digest="d" * 64,
        coordinate=NativeCoordinate(
            thread_id="thread-1",
            checkpoint_id="cp-2",
            checkpoint_ns="",
            task_id="task-2",
            interrupt_id="interrupt-2",
        ),
        descriptor_digest="b" * 64,
        grant_digest="c" * 64,
        publisher_binding_digest=publisher_binding_digest,
        consent_ref="consent:one",
        approval_generation=7,
        policy_epoch=11,
        config_epoch=13,
        parent_capability_generation=17,
        entries=tuple(
            PublicationEntry(artifact_ref=ref, destination=destination)
            for ref, destination in zip(refs, destinations, strict=True)
        ),
    )


def test_publication_prepare_is_side_effect_free_and_apply_is_exact(tmp_path: Path) -> None:
    from lockstep.runtime.publication import ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE", "two": b"TWO"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = ProjectPublisher(owner, project, registry, blobs)
    (project / "out").mkdir()
    handle = publisher.prepare(_request(
        refs, ("out/one.txt", "out/two.txt"),
        publisher_binding_digest=publisher.binding_digest,
    ))

    assert not (project / "out/one.txt").exists()
    receipt = publisher.apply_or_recover(handle)

    assert receipt.phase == "applied"
    assert (project / "out/one.txt").read_bytes() == b"ONE"
    assert (project / "out/two.txt").read_bytes() == b"TWO"
    assert publisher.apply_or_recover(handle) == receipt


@pytest.mark.parametrize("crash_after", [0, 1])
def test_publication_recovers_crash_after_each_atomic_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_after: int
) -> None:
    import lockstep.runtime.publication as publication

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE", "two": b"TWO"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = publication.ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(_request(
        refs, ("one.txt", "two.txt"),
        publisher_binding_digest=publisher.binding_digest,
    ))

    def crash(direction: str, index: int) -> None:
        if direction == "apply" and index == crash_after:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(publication, "_after_replacement", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        publisher.apply_or_recover(handle)
    monkeypatch.setattr(publication, "_after_replacement", lambda *_args: None)

    assert publisher.apply_or_recover(handle).phase == "applied"
    assert (project / "one.txt").read_bytes() == b"ONE"
    assert (project / "two.txt").read_bytes() == b"TWO"


def test_publication_rollback_recovers_from_partially_applied_journal(tmp_path: Path) -> None:
    import lockstep.runtime.publication as publication

    owner, blobs, registry, refs = _registry(
        tmp_path, {"new": b"NEW", "second": b"SECOND"}
    )
    project = tmp_path / "project"
    project.mkdir()
    target = project / "target.txt"
    target.write_bytes(b"OLD")
    publisher = publication.ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(_request(
        refs, ("target.txt", "second.txt"),
        publisher_binding_digest=publisher.binding_digest,
    ))

    def apply_crash(direction: str, index: int) -> None:
        if direction == "apply" and index == 0:
            raise RuntimeError("apply crash")

    publication._after_replacement = apply_crash
    with pytest.raises(RuntimeError, match="apply crash"):
        publisher.apply_or_recover(handle)

    def crash(direction: str, index: int) -> None:
        if direction == "rollback" and index == 0:
            raise RuntimeError("simulated crash")

    publication._after_replacement = crash
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            publisher.rollback_or_recover(handle)
    finally:
        publication._after_replacement = lambda *_args: None

    assert publisher.rollback_or_recover(handle).phase == "rolled_back"
    assert target.read_bytes() == b"OLD"
    assert not (project / "second.txt").exists()


def test_publication_rejects_collisions_git_controls_and_symlink_toctou(tmp_path: Path) -> None:
    from lockstep.runtime.publication import PublicationConflict, ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE", "two": b"TWO"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = ProjectPublisher(owner, project, registry, blobs)
    with pytest.raises((ValueError, PublicationConflict)):
        publisher.prepare(_request(
            refs, ("same.txt", "same.txt"),
            publisher_binding_digest=publisher.binding_digest,
        ))
    with pytest.raises((ValueError, PublicationConflict)):
        publisher.prepare(_request(
            refs[:1], (".git/config",),
            publisher_binding_digest=publisher.binding_digest,
        ))

    safe = project / "safe"
    safe.mkdir()
    handle = publisher.prepare(_request(
        refs[:1], ("safe/out.txt",),
        publisher_binding_digest=publisher.binding_digest,
    ))
    safe.rmdir()
    safe.symlink_to(tmp_path)
    with pytest.raises(PublicationConflict):
        publisher.apply_or_recover(handle)
    assert not (tmp_path / "out.txt").exists()


def test_corrupt_journal_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    from lockstep.runtime.publication import PublicationJournalError, ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(_request(
        refs, ("one.txt",), publisher_binding_digest=publisher.binding_digest
    ))
    journal = publisher.journal_path(handle)
    journal.chmod(0o600)
    journal.write_bytes(b"{not-json")
    before = journal.read_bytes()

    with pytest.raises(PublicationJournalError):
        publisher.apply_or_recover(handle)

    assert journal.read_bytes() == before
    assert not (project / "one.txt").exists()
