"""R1b-A0: fully granted static admission parks before native execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from lockstep import cli
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.catalog import RunCatalog
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.service import LockstepCommandService, preflight_recipe
from lockstep.runtime.start_service import plan_authorized_start
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


def _config(tmp_path: Path) -> dict[str, object]:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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


def _tree(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (str(path.relative_to(root)), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(("-wal", "-shm", "-journal"))
    )


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
    finally:
        store.close()
    assert not (owner_state / "checkpoints" / "native.sqlite").exists()


def test_granted_codex_static_admission_parks_before_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the positive static-policy branch must reject this granted run."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "codex-workflow", "codex")
    owner_state = tmp_path / "owner-state"
    granted = tuple(item.grant_selection_key for item in _requirements(project, "codex-workflow"))
    assert len(granted) == 1
    assert _provision(
        tmp_path, monkeypatch, project, owner_state, _config(tmp_path), granted, "codex-workflow"
    ) == 0

    result = _start(project, owner_state, "codex-workflow")

    _assert_prelaunch_park(owner_state, project, result)


def test_granted_pinned_static_admission_parks_before_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selector-specific pinned binding must use the same static park boundary."""

    project = tmp_path / "project"
    _write_direct_recipe(project, "pinned-workflow", "pinned")
    owner_state = tmp_path / "owner-state"
    granted = tuple(item.grant_selection_key for item in _requirements(project, "pinned-workflow"))
    assert len(granted) == 1
    assert _provision(
        tmp_path, monkeypatch, project, owner_state, _config(tmp_path), granted, "pinned-workflow"
    ) == 0

    result = _start(project, owner_state, "pinned-workflow")

    _assert_prelaunch_park(owner_state, project, result)


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
    before = _tree(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "target")

    assert _tree(owner_state) == before


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
    before = _tree(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "target")

    assert _tree(owner_state) == before


def test_acceptance_static_preflight_requires_no_publication_bearer(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    recipe_path = _write_acceptance_recipe(project)
    authorized = StrictRecipeIngress(recipe_path.parent).inspect(recipe_path.name).authorize(
        RecipeAuthorityPolicy()
    )
    owner_state = tmp_path / "owner-state"

    plan = plan_authorized_start(
        state_dir=owner_state,
        authorized=authorized,
        project=str(project),
        compiler_provenance=None,
        require_runtime_policy=LockstepCommandService._require_owner_runtime_policy,
    )

    assert _requirements(project, "acceptance") == ()
    assert plan.authorized is authorized
    assert not owner_state.exists()


def test_three_level_inventory_rejects_an_omitted_grandchild_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    _write_three_level_recipe(project)
    owner_state = tmp_path / "owner-state"
    requirements = _requirements(project, "root")
    assert {
        use
        for requirement in requirements
        for use in requirement.uses
    } == {
        ("root.recipe.yaml", "root-work"),
        ("child.yaml", "child-work"),
        ("grandchild.yaml", "grandchild-work"),
    }
    without_grandchild = tuple(
        requirement.grant_selection_key
        for requirement in requirements
        if ("grandchild.yaml", "grandchild-work") not in requirement.uses
    )

    assert _provision(
        tmp_path,
        monkeypatch,
        project,
        owner_state,
        _config(tmp_path),
        without_grandchild,
        "root",
    ) == 0
    before = _tree(owner_state)

    with pytest.raises(LockstepError):
        _start(project, owner_state, "root")

    assert _tree(owner_state) == before
