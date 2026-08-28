"""Owner-journal recovery remains bound to its exact project identity."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lockstep.authoring import project_paths
from lockstep.authoring_bundle import plan_project_compilation
from lockstep.authoring_publisher import AuthoringPublisher
from tests._authoring_gate import replace_marker, tree_image, write_workflow


class _ProcessDeath(BaseException):
    pass


def test_owner_journal_cannot_be_redirected_by_replacement_project_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    source = write_workflow(project, "leaf")
    owner = (tmp_path / "owner-state").resolve()
    publisher = AuthoringPublisher(owner)
    publisher.publish(plan_project_compilation(project_paths(project, "leaf")))
    replace_marker(source, "initial", "changed")
    bundle = plan_project_compilation(project_paths(project, "leaf"))
    destination_names = {image.resolved_path.name for image in bundle.after_images}
    original_replace = os.replace
    crashed = False
    def crash_after_destination(
        source_path: object,
        destination_path: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal crashed
        result = original_replace(source_path, destination_path, *args, **kwargs)
        is_destination = os.fsdecode(destination_path) in destination_names
        if not crashed and is_destination and kwargs.get("dst_dir_fd") is not None:
            crashed = True
            raise _ProcessDeath()
        return result
    monkeypatch.setattr(os, "replace", crash_after_destination)
    with pytest.raises(_ProcessDeath):
        publisher.publish(bundle)
    monkeypatch.setattr(os, "replace", original_replace)
    assert crashed
    replaced = project.with_name("project-replaced")
    project.rename(replaced)
    replaced_before = tree_image(replaced)
    project.mkdir()
    pointer = project / ".lockstep" / "authoring-recovery.json"
    pointer.parent.mkdir()
    pointer.write_text('{"project":"../project-replaced"}\n', encoding="utf-8")
    replacement_before = tree_image(project)
    owner_before = tree_image(owner)
    AuthoringPublisher(owner).recover(project)
    assert tree_image(project) == replacement_before
    assert tree_image(replaced) == replaced_before
    assert tree_image(owner) == owner_before
