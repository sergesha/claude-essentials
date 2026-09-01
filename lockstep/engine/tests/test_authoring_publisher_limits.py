"""Publisher rechecks every independent bound before project mutation."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lockstep.authoring_bundle import (
    AuthoringPlan,
    DirectoryIdentity,
    FileIdentity,
    PlannedTarget,
    SourceSnapshot,
)
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime.owner_state import StorageLimitExceeded
from tests._authoring_gate import tree_image


def _identity(path: Path) -> DirectoryIdentity:
    info = path.stat()
    return DirectoryIdentity(path, info.st_dev, info.st_ino)


def _file(path: Path) -> FileIdentity:
    info = path.lstat()
    return FileIdentity(
        info.st_dev, info.st_ino, info.st_mode, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _plan(
    tmp_path: Path, group: str, reads: int, writes: int, size: int
) -> AuthoringPlan:
    project = tmp_path / "project"
    inputs = project / ".lockstep/inputs"
    outputs = project / ".lockstep/recipes"
    inputs.mkdir(parents=True)
    outputs.mkdir()
    project_id = _identity(project)
    lockstep_id = _identity(project / ".lockstep")
    input_parents = (project_id, lockstep_id, _identity(inputs))
    output_parents = (project_id, lockstep_id, _identity(outputs))
    roles = tuple(f"role-{index}" for index in range(max(reads, writes)))
    sources = []
    for index, role in enumerate(roles[:reads]):
        content = b"r" * size if group == "read" else b"r"
        path = inputs / f"source-{index}"
        path.write_bytes(content)
        sources.append(SourceSnapshot(
            role, path, content, hashlib.sha256(content).hexdigest(),
            _file(path), input_parents,
        ))
    targets = []
    for index, role in enumerate(roles[:writes]):
        path = outputs / f"destination-{index}"
        before = b"b" * size if group == "before" else None
        before_file = None
        if before is not None:
            path.write_bytes(before)
            before_file = _file(path)
        after = b"a" * size if group == "after" else b"ok"
        targets.append(PlannedTarget(
            role, path, before,
            None if before is None else hashlib.sha256(before).hexdigest(),
            before_file, after, hashlib.sha256(after).hexdigest(),
            0o644, output_parents,
        ))
    return AuthoringPlan(
        project, project_id, tuple(sources),
        tuple((role, ()) for role in roles), tuple(targets),
    )


CASES = (
    ("read", 257, 1, 1, "read set exceeds 256"),
    ("paired", 0, 257, 1, "before images exceeds 256"),
    ("read", 5, 1, 900_000, "read set exceeds the aggregate byte"),
    ("before", 0, 5, 900_000, "before images exceeds the aggregate byte"),
    ("after", 0, 5, 900_000, "after images exceeds the aggregate byte"),
)


@pytest.mark.parametrize(("group", "reads", "writes", "size", "reason"), CASES)
def test_publisher_revalidates_each_limit_before_project_mutation_or_temporary(
    tmp_path, group, reads, writes, size, reason
) -> None:
    plan = _plan(tmp_path, group, reads, writes, size)
    owner = (tmp_path / "owner").resolve()
    before = tree_image(plan.project)
    with pytest.raises(StorageLimitExceeded, match=reason):
        AuthoringPublisher(owner).publish(plan)
    assert tree_image(plan.project) == before
    assert not tuple(plan.project.rglob(".lockstep-authoring-*.tmp"))
    assert not owner.exists()
