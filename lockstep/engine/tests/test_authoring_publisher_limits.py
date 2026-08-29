"""Publisher rechecks every independent bound before project mutation."""
from __future__ import annotations

import hashlib, stat
from pathlib import Path

import pytest

from lockstep.authoring_bundle import DestinationImage, LeafIdentity, PathIdentity, ProjectCompilationBundle, SourceIdentity
from lockstep.authoring_publisher import AuthoringPublisher
from lockstep.runtime.owner_state import StorageLimitExceeded
from tests._authoring_gate import tree_image


def _identity(path: Path) -> PathIdentity:
    info = path.stat(); return PathIdentity(path, info.st_dev, info.st_ino)


def _leaf(path: Path) -> LeafIdentity:
    info = path.lstat(); return LeafIdentity(path, info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _bundle(tmp_path: Path, group: str, reads: int, writes: int, size: int) -> ProjectCompilationBundle:
    project = tmp_path / "project"; inputs = project / ".lockstep/inputs"; outputs = project / ".lockstep/recipes"
    inputs.mkdir(parents=True); outputs.mkdir(); project_id = _identity(project)
    input_parents = (project_id, _identity(project / ".lockstep"), _identity(inputs))
    output_parents = (project_id, _identity(project / ".lockstep"), _identity(outputs))
    roles = tuple(f"role-{index}" for index in range(max(reads, writes)))
    sources = []
    for index, role in enumerate(roles[:reads]):
        content = b"r" * size if group == "read" else b"r"; path = inputs / f"source-{index}"; path.write_bytes(content)
        sources.append(SourceIdentity(role, path, content, hashlib.sha256(content).hexdigest(), _leaf(path), input_parents))
    before, after = [], []
    for index, role in enumerate(roles[:writes]):
        path = outputs / f"destination-{index}"; old = b"b" * size if group == "before" else None
        if old is None: before.append(DestinationImage(role, path, None, None, None, None, output_parents))
        else:
            path.write_bytes(old); leaf = _leaf(path)
            before.append(DestinationImage(role, path, old, hashlib.sha256(old).hexdigest(), stat.S_IMODE(leaf.mode), leaf, output_parents))
        new = b"a" * size if group == "after" else b"ok"
        after.append(DestinationImage(role, path, new, hashlib.sha256(new).hexdigest(), 0o644, None, output_parents))
    return ProjectCompilationBundle(project, project_id, tuple(sources), tuple((role, ()) for role in roles), tuple(before), tuple(after))


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
    bundle = _bundle(tmp_path, group, reads, writes, size); owner = (tmp_path / "owner").resolve()
    before = tree_image(bundle.resolved_project)
    with pytest.raises(StorageLimitExceeded, match=reason): AuthoringPublisher(owner).publish(bundle)
    assert tree_image(bundle.resolved_project) == before
    assert not tuple(bundle.resolved_project.rglob(".lockstep-authoring-*.tmp"))
    assert not owner.exists()
