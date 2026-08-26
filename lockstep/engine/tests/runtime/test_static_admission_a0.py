"""R1b-A0: fully granted static admission parks before native execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat

import pytest
from sqlalchemy import func, select

from lockstep import cli
from lockstep.runtime.catalog import RunCatalog
from lockstep.runtime.effects.coordinator import ProviderContractViolation
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime import service as service_module
from lockstep.runtime import start_service as start_service_module
from lockstep.runtime.service import preflight_recipe
from lockstep.runtime.storage import SQLiteStore


def _managed_effect(logical_id: str, selector: str) -> dict[str, object]:
    return {
        "schema": "lockstep.effect/v1",
        "kind": "managed",
        "logical_id": logical_id,
        "runner": {
            "selector": selector,
            "required_capabilities": ["workspace", "bounded_result"],
        },
        "inputs": {},
        "writes": [],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }


def _effect_node(logical_id: str, selector: str) -> dict[str, object]:
    return {
        "type": "interrupt",
        "message": {"lockstep_effect": _managed_effect(logical_id, selector)},
        "state_key": "request",
        "resume_key": "result",
        "idempotent": False,
    }


def _write_direct_recipe(project: Path, name: str, selector: str) -> None:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    document = {
        "version": "1.0",
        "name": name,
        "state": {"request": "dict", "result": "dict"},
        "nodes": {"work": _effect_node(f"{name}-work", selector)},
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "END"},
        ],
    }
    (recipes / f"{name}.recipe.yaml").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _write_three_level_recipe(project: Path) -> None:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    state = {"request": "dict", "result": "dict"}
    grandchild = {
        "version": "1.0",
        "name": "grandchild",
        "state": state,
        "nodes": {"work": _effect_node("grandchild-work", "codex")},
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "END"},
        ],
    }
    child = {
        "version": "1.0",
        "name": "child",
        "state": state,
        "nodes": {
            "work": _effect_node("child-work", "pinned"),
            "grandchild": {"type": "subgraph", "graph": "grandchild.yaml", "mode": "direct"},
        },
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "grandchild"},
            {"from": "grandchild", "to": "END"},
        ],
    }
    root = {
        "version": "1.0",
        "name": "root",
        "state": state,
        "nodes": {
            "work": _effect_node("root-work", "codex"),
            "child": {"type": "subgraph", "graph": "child.yaml", "mode": "direct"},
        },
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "child"},
            {"from": "child", "to": "END"},
        ],
    }
    for path, document in (
        (recipes / "root.recipe.yaml", root),
        (recipes / "child.yaml", child),
        (recipes / "grandchild.yaml", grandchild),
    ):
        path.write_text(json.dumps(document), encoding="utf-8")


def _write_acceptance_recipe(project: Path) -> Path:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    document = {
        "version": "1.0",
        "name": "acceptance",
        "state": {"review_result": "dict", "accepted": "dict"},
        "nodes": {
            "accept": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "accept",
                        "logical_id": "accept-review",
                        "artifact_handle": "review.report",
                        "producer_result_state_key": "review_result",
                        "declared_name": "report",
                        "destination": "docs/review.md",
                        "transformation": "identity",
                        "audience": "local-project",
                        "verdict": "PASS",
                        "result_schema": "lockstep.acceptance-result/v1",
                    }
                },
                "state_key": "review_result",
                "resume_key": "accepted",
                "idempotent": False,
            }
        },
        "edges": [
            {"from": "START", "to": "accept"},
            {"from": "accept", "to": "END"},
        ],
    }
    path = recipes / "acceptance.recipe.yaml"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _config(tmp_path: Path, *, provider_marker: Path | None = None) -> dict[str, object]:
    executable = tmp_path / "codex"
    marker = provider_marker or tmp_path / "provider-invoked"
    executable.write_text(
        "#!/bin/sh\nprintf invoked > " + shlex.quote(str(marker)) + "\nexit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    auth = codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    pinned_home = tmp_path / "pinned-home"
    pinned_home.mkdir(mode=0o700)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir(mode=0o700)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(private_tmp),
    }
    common = {
        "executable": str(executable),
        "model": "model",
        "cli_version": "version",
        "permission_profile": {"sandbox": "workspace-write", "approval": "never"},
        "environment": environment,
    }
    return {
        "schema": "lockstep.runtime-provision-config/v1",
        "codex": {**common, "codex_home": str(codex_home)},
        "pinned": {
            **common,
            "codex_home": str(pinned_home),
            "pinned_permission_profile": "owner-profile",
        },
    }


def _requirements(project: Path, *recipes: str):
    recipes_dir = project / ".lockstep" / "recipes"
    return RuntimeRequirementIndex.for_authorized_closures(
        tuple(preflight_recipe(recipes_dir, name) for name in recipes),
        project_identity=str(project.resolve()),
    ).requirements


def _provision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    owner_state: Path,
    config: dict[str, object],
    replacement: tuple[str, ...],
    *recipes: str,
) -> int:
    config_path = tmp_path / "runtime-config.json"
    grants_path = tmp_path / "runtime-grants.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    grants_path.write_text(json.dumps(replacement), encoding="utf-8")
    argv = [
        "owner",
        "provision-runtime",
        "--config",
        str(config_path),
        "--project",
        str(project),
    ]
    for recipe in recipes:
        argv.extend(("--recipe", recipe))
    argv.extend(("--replace-grants", str(grants_path)))
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    return cli.main(argv)


def _owner_state_snapshot(root: Path) -> tuple[tuple[str, str, int, bytes | str], ...]:
    """Capture every owner-state inode without following symlinks."""

    entries: list[tuple[str, str, int, bytes | str]] = []

    def visit(path: Path, relative: str) -> None:
        metadata = os.lstat(path)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, ""))
            with os.scandir(path) as children:
                for child in sorted(children, key=lambda entry: entry.name):
                    child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                    visit(Path(child.path), child_relative)
        elif stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "regular", mode, path.read_bytes()))
        elif stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, os.readlink(path)))
        else:
            inode_type = {
                stat.S_IFIFO: "fifo",
                stat.S_IFSOCK: "socket",
                stat.S_IFCHR: "character-device",
                stat.S_IFBLK: "block-device",
            }.get(stat.S_IFMT(metadata.st_mode), f"unknown:{stat.S_IFMT(metadata.st_mode):o}")
            entries.append((relative, inode_type, mode, ""))

    try:
        visit(root, ".")
    except FileNotFoundError:
        return ()
    return tuple(entries)


def _start(project: Path, owner_state: Path, recipe: str) -> dict[str, object]:
    service = Engine.command(owner_state, project / ".lockstep" / "recipes")
    try:
        return service.start(recipe, {}, str(project))
    finally:
        service.close()


def _assert_prelaunch_park(
    owner_state: Path,
    project: Path,
    result: dict[str, object],
    provider_marker: Path,
) -> None:
    run_id = result["run_id"]
    assert isinstance(run_id, str) and run_id
    store = SQLiteStore(owner_state / "runtime.sqlite")
    try:
        catalog = RunCatalog(store)
        assert [binding.public_run_id for binding in catalog.list(str(project.resolve()))] == [
            run_id
        ]
        with store.read_connection() as connection:
            assert connection.scalar(
                select(func.count()).select_from(store.tables.effect_dispatch_watches)
            ) == 1
            assert connection.scalar(
                select(func.count()).select_from(store.tables.effects)
            ) == 0
            assert connection.scalar(
                select(func.count()).select_from(store.tables.effect_observations)
            ) == 0
            assert connection.scalar(
                select(func.count()).select_from(store.tables.effect_runtime_inputs)
            ) == 0
            assert connection.scalar(
                select(func.count()).select_from(store.tables.publication_consents)
            ) == 0
    finally:
        store.close()
    assert not (owner_state / "checkpoints" / "native.sqlite").exists()
    assert not provider_marker.exists()


def test_granted_codex_static_admission_parks_before_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the positive static-policy branch must reject this granted run."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "codex-workflow", "codex")
    owner_state = tmp_path / "owner-state"
    provider_marker = tmp_path / "codex-provider-invoked"
    granted = tuple(item.grant_selection_key for item in _requirements(project, "codex-workflow"))
    assert len(granted) == 1
    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path, provider_marker=provider_marker),
        granted,
        "codex-workflow",
    ) == 0

    result = _start(project, owner_state, "codex-workflow")

    _assert_prelaunch_park(owner_state, project, result, provider_marker)


def test_granted_pinned_static_admission_parks_before_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selector-specific pinned binding must use the same static park boundary."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "pinned-workflow", "pinned")
    owner_state = tmp_path / "owner-state"
    provider_marker = tmp_path / "pinned-provider-invoked"
    granted = tuple(item.grant_selection_key for item in _requirements(project, "pinned-workflow"))
    assert len(granted) == 1
    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path, provider_marker=provider_marker),
        granted,
        "pinned-workflow",
    ) == 0

    result = _start(project, owner_state, "pinned-workflow")

    _assert_prelaunch_park(owner_state, project, result, provider_marker)


def test_granted_static_admission_remains_parked_after_service_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable recovery must preserve the pre-native A0 admission boundary."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "target", "codex")
    owner_state = tmp_path / "owner-state"
    provider_marker = tmp_path / "provider-invoked"
    granted = tuple(item.grant_selection_key for item in _requirements(project, "target"))
    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path, provider_marker=provider_marker),
        granted,
        "target",
    ) == 0
    result = _start(project, owner_state, "target")
    _assert_prelaunch_park(owner_state, project, result, provider_marker)


def test_repeated_recovery_classifies_one_immutable_static_admission_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive durable classification is bounded and reused only to park."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "target", "codex")
    owner_state = tmp_path / "owner-state"
    provider_marker = tmp_path / "provider-invoked"
    granted = tuple(item.grant_selection_key for item in _requirements(project, "target"))
    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path, provider_marker=provider_marker),
        granted,
        "target",
    ) == 0
    result = _start(project, owner_state, "target")

    original = start_service_module._is_static_runtime_admission
    classifications = 0

    def counted(binding, bundle_store):
        nonlocal classifications
        classifications += 1
        return original(binding, bundle_store)

    monkeypatch.setattr(
        start_service_module, "_is_static_runtime_admission", counted
    )
    monkeypatch.setattr(
        service_module, "_is_static_runtime_admission", counted, raising=False
    )
    reopened = Engine.command(owner_state, project / ".lockstep" / "recipes")
    try:
        reopened._open_writable_stores()
        for _ in range(4):
            reopened._recover_start_admissions()
    finally:
        reopened.close()

    assert classifications == 1
    _assert_prelaunch_park(owner_state, project, result, provider_marker)

    reopened = Engine.command(owner_state, project / ".lockstep" / "recipes")
    try:
        try:
            reopened._activate_writable_core()
        except ProviderContractViolation:
            # The regression oracle is the durable boundary below: recovery may
            # report a fail-closed error, but it may never cross into native work.
            pass
    finally:
        reopened.close()

    _assert_prelaunch_park(owner_state, project, result, provider_marker)


@pytest.mark.parametrize("configuration_only", (False, True))
def test_ungranted_or_configuration_only_runtime_start_is_write_free(
    configuration_only: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    _write_direct_recipe(project, "target", "codex")
    _write_direct_recipe(project, "other", "pinned")
    owner_state = tmp_path / "owner-state"
    replacement = () if configuration_only else tuple(
        item.grant_selection_key for item in _requirements(project, "other")
    )
    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path),
        replacement,
        "target",
        "other",
    ) == 0
    before = _owner_state_snapshot(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "target")

    assert _owner_state_snapshot(owner_state) == before


def test_real_captured_binding_drift_is_write_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    _write_direct_recipe(project, "target", "codex")
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    granted = tuple(item.grant_selection_key for item in _requirements(project, "target"))
    assert _provision(
        tmp_path, monkeypatch, project, owner_state, config, granted, "target"
    ) == 0
    codex = config["codex"]
    assert isinstance(codex, dict)
    Path(str(codex["codex_home"]), "auth.json").write_text(
        '{"rotated":true}', encoding="utf-8"
    )
    before = _owner_state_snapshot(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "target")

    assert _owner_state_snapshot(owner_state) == before


def test_unrelated_inconsistent_snapshot_grant_is_write_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening a snapshot validates every captured grant, not only the target."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "target", "codex")
    _write_direct_recipe(project, "other", "pinned")
    owner_state = tmp_path / "owner-state"
    target_keys = {
        item.grant_selection_key for item in _requirements(project, "target")
    }
    all_grants = tuple(
        item.grant_selection_key for item in _requirements(project, "target", "other")
    )
    assert len(target_keys) == 1
    assert len(all_grants) == 2
    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path),
        all_grants,
        "target",
        "other",
    ) == 0
    snapshot_path = owner_state / "runtime-owner" / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    unrelated = [
        grant
        for grant in snapshot["grants"]
        if grant["grant_selection_key"] not in target_keys
    ]
    assert len(unrelated) == 1
    unrelated[0]["requirement_digest"] = "0" * 64
    snapshot_path.write_text(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    before = _owner_state_snapshot(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "target")

    assert _owner_state_snapshot(owner_state) == before


def test_acceptance_static_inventory_requires_no_publication_bearer(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_acceptance_recipe(project)

    # Dynamic acceptance consent is covered by the service/effects suite;
    # static admission only owns whether this recipe demands runtime authority.
    assert _requirements(project, "acceptance") == ()


def test_three_level_inventory_rejects_an_omitted_grandchild_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    _write_three_level_recipe(project)
    owner_state = tmp_path / "owner-state"
    requirements = _requirements(project, "root")
    expected_uses = {
        ("root.recipe.yaml", "root-work"),
        ("child.yaml", "child-work"),
        ("grandchild.yaml", "grandchild-work"),
    }
    assert len(requirements) == 3
    by_use_partition = {requirement.uses: requirement for requirement in requirements}
    assert set(by_use_partition) == {(use,) for use in expected_uses}
    assert len({requirement.grant_selection_key for requirement in requirements}) == 3
    retained_uses = (
        (("root.recipe.yaml", "root-work"),),
        (("child.yaml", "child-work"),),
    )
    expected_retained_keys = {
        by_use_partition[uses].grant_selection_key for uses in retained_uses
    }
    without_grandchild = tuple(
        requirement.grant_selection_key
        for requirement in requirements
        if requirement.grant_selection_key in expected_retained_keys
    )
    assert len(without_grandchild) == 2
    assert set(without_grandchild) == expected_retained_keys

    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path),
        without_grandchild,
        "root",
    ) == 0
    snapshot = json.loads(
        (owner_state / "runtime-owner" / "snapshot.json").read_text(encoding="utf-8")
    )
    assert len(snapshot["grants"]) == 2
    assert {grant["grant_selection_key"] for grant in snapshot["grants"]} == set(
        without_grandchild
    )
    before = _owner_state_snapshot(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "root")

    assert _owner_state_snapshot(owner_state) == before
