"""R1b-A0: exact static admission before any start-side persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat

import pytest

from lockstep import cli
from lockstep.runtime.effects.owner_policy import (
    RuntimeAdmissionDecision,
    RuntimeRequirementIndex,
    requirement_digest,
)
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.service import preflight_recipe
from lockstep.runtime.start_service import (
    _preflight_runtime_requirements,
    plan_authorized_start,
)


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


def _assert_exact_static_plan(
    owner_state: Path,
    project: Path,
    recipe: str,
    selector: str,
    granted: tuple[str, ...],
    provider_marker: Path,
) -> None:
    authorized = preflight_recipe(project / ".lockstep" / "recipes", recipe)
    expected_index = RuntimeRequirementIndex.for_authorized_closure(
        authorized,
        project_identity=str(project.resolve()),
    )
    assert len(expected_index.requirements) == 1
    expected_requirement = expected_index.requirements[0]
    snapshot_digest, snapshot = open_runtime_snapshot(owner_state)
    before = _owner_state_snapshot(owner_state)

    plan = plan_authorized_start(
        state_dir=owner_state,
        authorized=authorized,
        project=str(project),
        compiler_provenance=None,
        require_runtime_policy=lambda index: _preflight_runtime_requirements(
            owner_state, index
        ),
    )

    decision = plan.runtime_admission
    assert isinstance(decision, RuntimeAdmissionDecision)
    assert plan.authorized == authorized
    assert plan.project_root == project.resolve()
    assert decision.snapshot_digest == snapshot_digest
    assert decision.snapshot == snapshot
    assert len(decision.requirements) == 1
    requirement, bound_digest, grant = decision.requirements[0]
    binding = snapshot.codex if selector == "codex" else snapshot.pinned
    assert requirement == expected_requirement
    assert requirement.runner_selector == selector
    assert granted == (requirement.grant_selection_key,)
    assert bound_digest == requirement_digest(
        grant_selection_key=requirement.grant_selection_key,
        runner_binding_digest=binding.binding_digest,
        config_generation=snapshot.config_generation,
    )
    assert grant == next(
        item
        for item in snapshot.grants
        if item.grant_selection_key == requirement.grant_selection_key
    )
    assert grant.requirement_digest == bound_digest
    assert grant.config_generation == snapshot.config_generation
    assert grant.policy_generation == snapshot.policy_generation
    assert grant.grant_generation > 0
    assert _owner_state_snapshot(owner_state) == before
    assert not (owner_state / "runtime.sqlite").exists()
    assert not (owner_state / "checkpoints" / "native.sqlite").exists()
    assert not provider_marker.exists()


def test_granted_codex_static_planning_returns_exact_admission_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Codex provisioning binds one exact write-free admission decision."""

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

    _assert_exact_static_plan(
        owner_state,
        project,
        "codex-workflow",
        "codex",
        granted,
        provider_marker,
    )


def test_granted_pinned_static_planning_returns_exact_admission_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real pinned provisioning binds one exact write-free admission decision."""

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

    _assert_exact_static_plan(
        owner_state,
        project,
        "pinned-workflow",
        "pinned",
        granted,
        provider_marker,
    )


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
