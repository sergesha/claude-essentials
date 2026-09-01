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
        authority_request_digest="e" * 64,
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


def _finish_apply(publisher, handle):
    for _ in range(2 * 32 + 2):
        receipt = publisher.apply_or_recover(handle)
        if receipt.phase == "applied":
            return receipt
    raise AssertionError("publication did not converge within its hard bound")


def _finish_rollback(publisher, handle):
    for _ in range(2 * 32 + 2):
        receipt = publisher.rollback_or_recover(handle)
        if receipt.phase == "rolled_back":
            return receipt
    raise AssertionError("rollback did not converge within its hard bound")


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
    receipt = _finish_apply(publisher, handle)

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
        _finish_apply(publisher, handle)
    monkeypatch.setattr(publication, "_after_replacement", lambda *_args: None)

    assert _finish_apply(publisher, handle).phase == "applied"
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
        _finish_apply(publisher, handle)

    def crash(direction: str, index: int) -> None:
        if direction == "rollback" and index == 0:
            raise RuntimeError("simulated crash")

    publication._after_replacement = crash
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            _finish_rollback(publisher, handle)
    finally:
        publication._after_replacement = lambda *_args: None

    assert _finish_rollback(publisher, handle).phase == "rolled_back"
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
        _finish_apply(publisher, handle)
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
        _finish_apply(publisher, handle)

    assert journal.read_bytes() == before
    assert not (project / "one.txt").exists()


def test_publication_rejects_aggregate_bytes_before_journal_or_replacement(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.owner_state import StorageLimitExceeded
    from lockstep.runtime.publication import PublicationLimits, ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE", "two": b"TWO"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = ProjectPublisher(
        owner,
        project,
        registry,
        blobs,
        limits=PublicationLimits(max_total_bytes=5),
    )

    with pytest.raises(StorageLimitExceeded, match="aggregate"):
        publisher.prepare(
            _request(
                refs,
                ("one.txt", "two.txt"),
                publisher_binding_digest=publisher.binding_digest,
            )
        )
    assert not tuple((owner / "publications" / publisher.binding_digest / "journals").iterdir())
    assert not (project / "one.txt").exists()


def test_publication_aggregate_limit_includes_existing_preimages(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.owner_state import StorageLimitExceeded
    from lockstep.runtime.publication import PublicationLimits, ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"N"})
    project = tmp_path / "project"
    project.mkdir()
    target = project / "one.txt"
    target.write_bytes(b"OLD!!")
    publisher = ProjectPublisher(
        owner,
        project,
        registry,
        blobs,
        limits=PublicationLimits(max_total_bytes=5),
    )

    with pytest.raises(StorageLimitExceeded, match="aggregate"):
        publisher.prepare(
            _request(
                refs,
                ("one.txt",),
                publisher_binding_digest=publisher.binding_digest,
            )
        )
    assert target.read_bytes() == b"OLD!!"
    assert not tuple((owner / "publications" / publisher.binding_digest / "journals").iterdir())


def test_publication_rechecks_preimage_at_atomic_replacement_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lockstep.runtime.publication import PublicationConflict, ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE"})
    project = tmp_path / "project"
    project.mkdir()
    target = project / "one.txt"
    target.write_bytes(b"OLD")
    publisher = ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(
        _request(
            refs,
            ("one.txt",),
            publisher_binding_digest=publisher.binding_digest,
        )
    )
    original = publisher._current_image
    observations = 0

    def current_after_foreign_write(parent_fd, leaf):
        nonlocal observations
        observations += 1
        if observations == 2:
            target.write_bytes(b"ALIEN")
        return original(parent_fd, leaf)

    monkeypatch.setattr(publisher, "_current_image", current_after_foreign_write)
    with pytest.raises(PublicationConflict, match="destination changed"):
        publisher.apply_or_recover(handle)
    assert target.read_bytes() == b"ALIEN"


def test_publication_reverifies_whole_set_before_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lockstep.runtime.publication as publication

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE", "two": b"TWO"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = publication.ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(
        _request(
            refs,
            ("one.txt", "two.txt"),
            publisher_binding_digest=publisher.binding_digest,
        )
    )

    def mutate_first_after_second(direction: str, index: int) -> None:
        if direction == "apply" and index == 1:
            (project / "one.txt").write_bytes(b"ALIEN")

    monkeypatch.setattr(publication, "_after_replacement", mutate_first_after_second)
    with pytest.raises(publication.PublicationConflict, match="changed"):
        _finish_apply(publisher, handle)
    assert (project / "one.txt").read_bytes() == b"ALIEN"


def test_terminal_receipt_reverifies_bytes_and_preimage_mode(tmp_path: Path) -> None:
    import os
    from lockstep.runtime.publication import PublicationConflict, ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"NEW"})
    project = tmp_path / "project"
    project.mkdir()
    target = project / "one.txt"
    target.write_bytes(b"OLD")
    target.chmod(0o640)
    publisher = ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(
        _request(refs, ("one.txt",), publisher_binding_digest=publisher.binding_digest)
    )
    assert _finish_apply(publisher, handle).phase == "applied"
    target.write_bytes(b"ALIEN")
    with pytest.raises(PublicationConflict, match="changed"):
        publisher.apply_or_recover(handle)
    target.write_bytes(b"NEW")
    target.chmod(0o600)

    mode_target = project / "mode.txt"
    mode_target.write_bytes(b"OLD")
    mode_target.chmod(0o640)
    mode_handle = publisher.prepare(
        _request(refs, ("mode.txt",), publisher_binding_digest=publisher.binding_digest)
    )
    assert publisher.apply_or_recover(mode_handle).phase == "applying"
    assert publisher.apply_or_recover(mode_handle).phase == "applying"
    assert _finish_rollback(publisher, mode_handle).phase == "rolled_back"
    assert mode_target.read_bytes() == b"OLD"
    assert os.stat(mode_target).st_mode & 0o777 == 0o640


def test_publication_advances_at_most_one_monotonic_action_per_call(
    tmp_path: Path,
) -> None:
    from lockstep.runtime.publication import ProjectPublisher

    owner, blobs, registry, refs = _registry(tmp_path, {"one": b"ONE", "two": b"TWO"})
    project = tmp_path / "project"
    project.mkdir()
    publisher = ProjectPublisher(owner, project, registry, blobs)
    handle = publisher.prepare(
        _request(refs, ("one.txt", "two.txt"), publisher_binding_digest=publisher.binding_digest)
    )
    assert publisher.apply_or_recover(handle).phase == "applying"
    assert (project / "one.txt").read_bytes() == b"ONE"
    assert not (project / "two.txt").exists()
    assert publisher.apply_or_recover(handle).phase == "applying"
    assert not (project / "two.txt").exists()
    assert publisher.apply_or_recover(handle).phase == "applying"
    assert (project / "two.txt").read_bytes() == b"TWO"
