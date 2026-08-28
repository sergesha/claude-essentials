"""Public authoring publisher contract."""

from __future__ import annotations

import stat
from pathlib import Path

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher

from tests._authoring_gate import tree_image, write_workflow


def test_successful_publish_materializes_only_exact_bundle_destinations(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    sentinel = project / "notes" / "owner.txt"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"owner bytes\n")
    sentinel.chmod(0o640)
    sentinel_before = tree_image(sentinel)
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    source_before = tree_image(source)
    project_files_before = {
        path.resolve()
        for path in project.rglob("*")
        if not path.is_dir() or path.is_symlink()
    }
    expected_destinations = {
        image.resolved_path for image in bundle.after_images
    }
    assert sentinel.resolve() not in expected_destinations
    owner_state = (tmp_path / "owner-state").resolve()

    AuthoringPublisher(owner_state).publish(bundle)

    for image in bundle.after_images:
        destination_stat = image.resolved_path.lstat()
        assert stat.S_ISREG(destination_stat.st_mode)
        assert image.resolved_path.read_bytes() == image.content
        assert stat.S_IMODE(destination_stat.st_mode) == image.mode
    assert tree_image(source) == source_before
    assert tree_image(sentinel) == sentinel_before
    assert {
        path.resolve()
        for path in project.rglob("*")
        if not path.is_dir() or path.is_symlink()
    } == project_files_before | expected_destinations
