"""Public template installation through the whole-DAG authoring transaction."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import stat
import time
from pathlib import Path
from typing import NoReturn

import pytest

from lockstep import cli, templates
from lockstep.authoring_bundle import ProjectCompilationBundle
from lockstep.authoring_publisher import AuthoringPublisher

from tests._authoring_crash_gate import (
    NamespaceEntry,
    namespace_entry,
    namespace_image,
)


def _invoke_template_init(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project: Path,
    owner_state: Path,
) -> tuple[int, str, str]:
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    monkeypatch.chdir(project)
    returncode = cli.main(["template", "init", "reviewed-change", "release"])
    captured = capsys.readouterr()
    return returncode, captured.out, captured.err


def _is_destination_mutation(
    destinations: tuple[Path, ...], destination: object, directory_fd: int | None
) -> bool:
    if directory_fd is None:
        return Path(os.fsdecode(destination)).resolve() in destinations
    leaf = os.fsdecode(destination)
    directory_info = os.fstat(directory_fd)
    return any(
        path.name == leaf
        and (path.parent.stat().st_dev, path.parent.stat().st_ino)
        == (directory_info.st_dev, directory_info.st_ino)
        for path in destinations
    )


def _run_crashing_template_child(
    project: Path,
    owner_state: Path,
    destinations: tuple[Path, ...],
    *,
    crash_exit: int,
    error_exit: int,
) -> NoReturn:
    try:
        os.environ["LOCKSTEP_STATE_DIR"] = str(owner_state)
        os.chdir(project)
        original_link = os.link
        original_replace = os.replace

        def link_then_die(source, destination, *args, **kwargs):
            result = original_link(source, destination, *args, **kwargs)
            if _is_destination_mutation(
                destinations, destination, kwargs.get("dst_dir_fd")
            ):
                os._exit(crash_exit)
            return result

        os.link = link_then_die

        def replace_then_die(source, destination, *args, **kwargs):
            result = original_replace(source, destination, *args, **kwargs)
            if _is_destination_mutation(
                destinations, destination, kwargs.get("dst_dir_fd")
            ):
                os._exit(crash_exit)
            return result

        os.replace = replace_then_die
        cli.main(["template", "init", "reviewed-change", "release"])
    except BaseException:
        os._exit(error_exit)
    os._exit(error_exit)


def _crash_after_first_destination(
    project: Path, owner_state: Path, destinations: tuple[Path, ...]
) -> tuple[bool, dict[str, NamespaceEntry]]:
    crash_exit = 86
    child = os.fork()
    if child == 0:
        _run_crashing_template_child(
            project,
            owner_state,
            destinations,
            crash_exit=crash_exit,
            error_exit=87,
        )
    deadline = time.monotonic() + 10.0
    interval = 0.01
    while True:
        waited_child, status = os.waitpid(child, os.WNOHANG)
        if waited_child == child:
            break
        if time.monotonic() >= deadline:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            raise AssertionError("template crash child exceeded 10 seconds")
        time.sleep(interval)
        interval = min(interval * 2, 0.1)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == crash_exit
    legacy = project / ".lockstep" / ".template-install.json"
    return (
        legacy.exists() or legacy.is_symlink(),
        namespace_image(owner_state)
        if owner_state.exists() and not owner_state.is_symlink()
        else {},
    )


def _reference_install_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict[Path, bytes]:
    reference = tmp_path / "reference"
    reference.mkdir()
    assert _invoke_template_init(
        monkeypatch,
        capsys,
        reference,
        (tmp_path / "reference-owner").resolve(),
    ) == (0, "initialized release\n", "")
    expected = {
        path.relative_to(reference): path.read_bytes()
        for path in reference.rglob("*")
        if path.is_file()
    }
    assert expected
    return expected


def test_direct_template_install_requires_explicit_external_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_parameter = inspect.signature(templates.install_template).parameters[
        "state_dir"
    ]
    assert state_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert state_parameter.default is inspect.Parameter.empty
    project = tmp_path / "project"
    project.mkdir()
    owner_state = (tmp_path / "owner-state").resolve()
    constructed_with: list[Path] = []
    events: list[tuple[str, AuthoringPublisher, object]] = []
    original_init = AuthoringPublisher.__init__
    original_recover = AuthoringPublisher.recover
    original_publish = AuthoringPublisher.publish

    def initialize(publisher: AuthoringPublisher, state: Path) -> None:
        constructed_with.append(state)
        original_init(publisher, state)

    def recover(publisher: AuthoringPublisher, observed_project: Path) -> None:
        events.append(("recover", publisher, observed_project))
        original_recover(publisher, observed_project)

    def publish(
        publisher: AuthoringPublisher, bundle: ProjectCompilationBundle
    ) -> None:
        assert bundle.sources == ()
        events.append(("publish", publisher, bundle))
        original_publish(publisher, bundle)

    monkeypatch.setattr(AuthoringPublisher, "__init__", initialize)
    monkeypatch.setattr(AuthoringPublisher, "recover", recover)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)

    installed = templates.install_template(
        "reviewed-change", "release", project, state_dir=owner_state
    )

    assert constructed_with == [owner_state]
    assert [event[0] for event in events] == ["recover", "publish"]
    assert events[0][1] is events[1][1]
    assert events[0][2] == project
    assert all(path.is_file() for path in (*installed.sources, *installed.recipes))


def test_cli_template_init_routes_one_destination_only_bundle_through_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sentinel = project / "foreign.bin"
    sentinel.write_bytes(b"foreign project bytes\n")
    sentinel_before = namespace_entry(sentinel)
    owner_state = (tmp_path / "owner-state").resolve()
    project_before = namespace_image(project)
    constructor_states: list[Path] = []
    events: list[tuple[str, object]] = []
    original_init = AuthoringPublisher.__init__
    original_recover = AuthoringPublisher.recover
    original_publish = AuthoringPublisher.publish

    def initialize(publisher: AuthoringPublisher, state: Path) -> None:
        constructor_states.append(state)
        original_init(publisher, state)

    def recover(publisher: AuthoringPublisher, observed_project: Path) -> None:
        events.append(("recover", observed_project))
        original_recover(publisher, observed_project)

    def publish(
        publisher: AuthoringPublisher, bundle: ProjectCompilationBundle
    ) -> None:
        assert namespace_image(project) == project_before
        events.append(("publish", bundle))
        original_publish(publisher, bundle)

    monkeypatch.setattr(AuthoringPublisher, "__init__", initialize)
    monkeypatch.setattr(AuthoringPublisher, "recover", recover)
    monkeypatch.setattr(AuthoringPublisher, "publish", publish)

    result = _invoke_template_init(
        monkeypatch, capsys, project, owner_state
    )

    assert result == (0, "initialized release\n", "")
    assert constructor_states == [owner_state]
    assert [event[0] for event in events] == ["recover", "publish"]
    assert events[0][1] == project
    bundle = events[1][1]
    assert isinstance(bundle, ProjectCompilationBundle)
    assert bundle.resolved_project == project.resolve()
    assert bundle.sources == ()
    assert bundle.dependency_edges == (
        ("release-review", ()),
        ("release", ("release-review",)),
    )
    assert all(image.content is None for image in bundle.before_images)
    assert all(image.content is not None for image in bundle.after_images)
    published_paths = {image.resolved_path for image in bundle.after_images}
    final_paths = {
        path.resolve()
        for path in project.rglob("*")
        if path.is_file() and path != sentinel
    }
    assert published_paths == final_paths
    assert namespace_entry(sentinel) == sentinel_before


@pytest.mark.parametrize("legacy_kind", ("valid", "malformed", "symlink"))
def test_cli_template_init_preserves_legacy_project_journal_as_inert_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    legacy_kind: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    foreign = project / "foreign.bin"
    foreign.write_bytes(b"foreign project bytes\n")
    legacy = project / ".lockstep" / ".template-install.json"
    legacy.parent.mkdir()
    valid_payload = json.dumps(
        {
            "schema": "lockstep.template-install/v1",
            "entries": [
                {
                    "path": "foreign.bin",
                    "sha256": hashlib.sha256(foreign.read_bytes()).hexdigest(),
                }
            ],
        },
        sort_keys=True,
    ).encode()
    observed_paths = [foreign, legacy]
    if legacy_kind == "valid":
        legacy.write_bytes(valid_payload)
    elif legacy_kind == "malformed":
        legacy.write_bytes(b"{not-json")
    else:
        legacy_target = tmp_path / "legacy-target.json"
        legacy_target.write_bytes(valid_payload)
        legacy_target.chmod(0o640)
        legacy.symlink_to(legacy_target)
        observed_paths.append(legacy_target)
    before = {
        path: namespace_entry(path)
        for path in observed_paths
    }

    result = _invoke_template_init(
        monkeypatch,
        capsys,
        project,
        (tmp_path / "owner-state").resolve(),
    )

    assert result == (0, "initialized release\n", "")
    assert {path: namespace_entry(path) for path in observed_paths} == before


def test_cli_template_init_rejects_nonbasic_artifact_collision_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    collision = project / ".lockstep" / "recipes" / "release.dependencies.json"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"foreign dependency bytes\n")
    collision.chmod(0o640)
    before = namespace_image(project)

    result = _invoke_template_init(
        monkeypatch,
        capsys,
        project,
        (tmp_path / "owner-state").resolve(),
    )

    assert result[0] == 2
    assert result[1] == ""
    assert "release.dependencies.json" in result[2]
    assert namespace_image(project) == before


def test_cli_template_init_recovers_real_process_death_before_reinstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not hasattr(os, "fork"):
        pytest.skip("real template process-death oracle requires POSIX fork")
    expected = _reference_install_image(tmp_path, monkeypatch, capsys)

    project = tmp_path / "project"
    project.mkdir()
    sentinel = project / "foreign.bin"
    sentinel.write_bytes(b"foreign project bytes\n")
    sentinel_before = namespace_entry(sentinel)
    destinations = tuple(project / relative for relative in expected)
    for parent in {path.parent for path in destinations}:
        parent.mkdir(parents=True, exist_ok=True)
    owner_state = (tmp_path / "owner-state").resolve()
    legacy_after_crash, owner_after_crash = _crash_after_first_destination(
        project, owner_state, destinations
    )
    mixed = tuple(path for path in destinations if path.is_file())
    assert len(mixed) == 1
    mixed_relative = mixed[0].relative_to(project)
    assert mixed[0].read_bytes() == expected[mixed_relative]
    assert stat.S_IMODE(mixed[0].lstat().st_mode) == 0o644
    publish_preimages: list[bool] = []
    original_publish = AuthoringPublisher.publish

    def publish_after_recovery(
        publisher: AuthoringPublisher, bundle: ProjectCompilationBundle
    ) -> None:
        assert bundle.sources == ()
        assert {
            image.resolved_path for image in bundle.after_images
        } == set(destinations)
        assert all(image.content is None for image in bundle.before_images)
        publish_preimages.append(
            all(not path.exists() and not path.is_symlink() for path in destinations)
        )
        original_publish(publisher, bundle)

    monkeypatch.setattr(AuthoringPublisher, "publish", publish_after_recovery)
    result = _invoke_template_init(
        monkeypatch, capsys, project, owner_state
    )

    assert result == (0, "initialized release\n", "")
    assert not legacy_after_crash
    assert any(
        entry.kind == "regular" and entry.mode & 0o077 == 0
        for entry in owner_after_crash.values()
    )
    assert publish_preimages == [True]
    final_image = namespace_image(project)
    final_payload = {
        Path(relative): entry
        for relative, entry in final_image.items()
        if entry.kind != "directory" and relative != "foreign.bin"
    }
    assert set(final_payload) == set(expected)
    assert all(
        entry.kind == "regular"
        and entry.content == expected[relative]
        and entry.mode == 0o644
        for relative, entry in final_payload.items()
    )
    assert namespace_entry(sentinel) == sentinel_before
    legacy = project / ".lockstep" / ".template-install.json"
    assert not legacy.exists() and not legacy.is_symlink()
