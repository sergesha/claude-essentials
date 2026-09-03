"""R1b-P: real complete-replacement owner snapshot provisioning."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from lockstep.runtime.advisory_lock import advisory_file_lock
from lockstep.runtime.effects.owner_policy import (
    RuntimeProvisioningInventory,
    RuntimeRequirementIndex,
    requirement_digest,
)
from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
from lockstep.runtime.owner_state import ensure_owner_directory
from lockstep.runtime.providers.codex import CodexInstallationBinding
from lockstep.runtime.service import preflight_recipe

from lockstep import cli


def _effect_node(logical_id: str, *, selector: str = "codex") -> dict[str, object]:
    return {
        "type": "interrupt",
        "message": {
            "lockstep_effect": {
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
        },
        "state_key": "request",
        "resume_key": "result",
        "idempotent": False,
    }


def _write_recipe(
    project: Path,
    name: str,
    logical_id: str,
    *,
    selector: str = "codex",
) -> None:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    document = {
        "version": "1.0",
        "name": name,
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "work": _effect_node(logical_id, selector=selector),
        },
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "END"},
        ],
    }
    (recipes / f"{name}.recipe.yaml").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _write_coalesced_recipe(project: Path) -> None:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    shared_state = {"request": "dict", "result": "dict"}
    child = {
        "version": "1.0",
        "name": "child",
        "state": shared_state,
        "nodes": {"work": _effect_node("shared-work")},
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "END"},
        ],
    }
    root = {
        "version": "1.0",
        "name": "root",
        "state": shared_state,
        "nodes": {
            "work": _effect_node("shared-work"),
            "child": {
                "type": "subgraph",
                "graph": "child.yaml",
                "mode": "direct",
            },
        },
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "child"},
            {"from": "child", "to": "END"},
        ],
    }
    (recipes / "root.recipe.yaml").write_text(json.dumps(root), encoding="utf-8")
    (recipes / "child.yaml").write_text(json.dumps(child), encoding="utf-8")


def _config(tmp_path: Path, *, model: str = "model") -> dict[str, object]:
    executable = tmp_path / "codex"
    if not executable.exists():
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700, exist_ok=True)
    auth = codex_home / "auth.json"
    if not auth.exists():
        auth.write_text("{}", encoding="utf-8")
        auth.chmod(0o600)
    pinned_home = tmp_path / "pinned-home"
    pinned_home.mkdir(mode=0o700, exist_ok=True)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir(mode=0o700, exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(private_tmp),
    }
    common = {
        "executable": str(executable),
        "model": model,
        "cli_version": "version",
        "permission_profile": {
            "sandbox": "workspace-write",
            "approval": "never",
        },
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


def _keys(project: Path, *recipes: str) -> tuple[str, ...]:
    recipes_dir = project / ".lockstep" / "recipes"
    index = RuntimeRequirementIndex.for_authorized_closures(
        tuple(preflight_recipe(recipes_dir, name) for name in recipes),
        project_identity=str(project.resolve()),
    )
    return tuple(item.grant_selection_key for item in index.requirements)


def _argv(project: Path, config: Path, grants: Path, *recipes: str) -> list[str]:
    argv = [
        "owner",
        "provision-runtime",
        "--config",
        str(config),
        "--project",
        str(project),
    ]
    for recipe in recipes:
        argv.extend(("--recipe", recipe))
    argv.extend(("--replace-grants", str(grants)))
    return argv


def _provision(
    tmp_path: Path,
    project: Path,
    owner_state: Path,
    config_value: dict[str, object],
    replacement: tuple[str, ...],
    *recipes: str,
) -> int:
    config = tmp_path / f"config-{time.monotonic_ns()}.json"
    grants = tmp_path / f"grants-{time.monotonic_ns()}.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants.write_text(json.dumps(replacement), encoding="utf-8")
    previous = os.environ.get("LOCKSTEP_STATE_DIR")
    os.environ["LOCKSTEP_STATE_DIR"] = str(owner_state)
    try:
        return cli.main(_argv(project, config, grants, *recipes))
    finally:
        if previous is None:
            os.environ.pop("LOCKSTEP_STATE_DIR", None)
        else:
            os.environ["LOCKSTEP_STATE_DIR"] = previous


def _snapshot(owner_state: Path) -> tuple[bytes, dict[str, object]]:
    encoded = (owner_state / "runtime-owner" / "snapshot.json").read_bytes()
    return encoded, json.loads(encoded)


def _grants(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    values = snapshot["grants"]
    assert isinstance(values, list)
    return {item["grant_selection_key"]: item for item in values}


def _pinned_binding_digest(
    installation_digest: str,
    permission_profile: str,
) -> str:
    encoded = json.dumps(
        {
            "schema": "lockstep.pinned-runner-binding/v1",
            "installation_digest": installation_digest,
            "permission_profile": permission_profile,
            "execution_authority": "os_user_execution",
            "deployment_profile": "local_unsandboxed",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _subprocess_result(
    argv: list[str],
    *,
    owner_state: Path,
    timeout: float = 2.0,
) -> int:
    process = subprocess.Popen(
        [sys.executable, "-m", "lockstep", *argv],
        env={**os.environ, "LOCKSTEP_STATE_DIR": str(owner_state)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        pytest.fail("owner provisioning blocked on a non-regular file")


def test_equal_inputs_are_byte_for_byte_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    selected = _keys(project, "sample")

    assert _provision(tmp_path, project, owner_state, config, selected, "sample") == 0
    first_bytes, first = _snapshot(owner_state)
    assert _provision(tmp_path, project, owner_state, config, selected, "sample") == 0
    second_bytes, second = _snapshot(owner_state)

    assert second_bytes == first_bytes
    assert second["config_generation"] == first["config_generation"] == 1
    assert second["policy_generation"] == first["policy_generation"] == 1
    assert _grants(second)[selected[0]]["grant_generation"] == 1


def test_multi_project_inventory_is_one_deterministic_exact_union(
    tmp_path: Path,
) -> None:
    projects = (tmp_path / "z-project", tmp_path / "a-project")
    indexes = []
    for project, name in zip(projects, ("z", "a"), strict=True):
        _write_recipe(project, name, f"{name}-work")
        indexes.append(
            RuntimeRequirementIndex.for_authorized_closure(
                preflight_recipe(project / ".lockstep" / "recipes", name),
                project_identity=str(project.resolve()),
            )
        )

    forward = RuntimeProvisioningInventory.combine(tuple(indexes))
    reverse = RuntimeProvisioningInventory.combine(tuple(reversed(indexes)))
    assert forward == reverse
    assert forward.project_identities == tuple(
        sorted(str(project.resolve()) for project in projects)
    )
    expected_keys = tuple(
        sorted(
            requirement.grant_selection_key
            for index in indexes
            for requirement in index.requirements
        )
    )
    assert tuple(
        requirement.grant_selection_key for requirement in forward.requirements
    ) == expected_keys

    config = _config(tmp_path)
    snapshot = provision_runtime_snapshot(
        state_dir=tmp_path / "owner-state",
        codex=config["codex"],
        pinned=config["pinned"],
        replacement_keys=expected_keys,
        index=forward,
        project=projects[0],
    )
    assert tuple(grant.grant_selection_key for grant in snapshot.grants) == expected_keys


def test_multi_project_inventory_checks_every_project_boundary(tmp_path: Path) -> None:
    projects = (tmp_path / "first", tmp_path / "second")
    indexes = []
    for project, name in zip(projects, ("first", "second"), strict=True):
        _write_recipe(project, name, f"{name}-work")
        indexes.append(
            RuntimeRequirementIndex.for_authorized_closure(
                preflight_recipe(project / ".lockstep" / "recipes", name),
                project_identity=str(project.resolve()),
            )
        )
    inventory = RuntimeProvisioningInventory.combine(tuple(indexes))
    keys = tuple(item.grant_selection_key for item in inventory.requirements)
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="outside project"):
        provision_runtime_snapshot(
            state_dir=projects[1] / "owner-state",
            codex=config["codex"],
            pinned=config["pinned"],
            replacement_keys=keys,
            index=inventory,
            project=projects[0],
        )

    inside_second = projects[1] / "runtime-tmp"
    inside_second.mkdir(mode=0o700)
    for selector in ("codex", "pinned"):
        binding = config[selector]
        assert isinstance(binding, dict)
        environment = binding["environment"]
        assert isinstance(environment, dict)
        environment["TMPDIR"] = str(inside_second)
    with pytest.raises(ValueError, match="TMPDIR"):
        provision_runtime_snapshot(
            state_dir=tmp_path / "owner-state",
            codex=config["codex"],
            pinned=config["pinned"],
            replacement_keys=keys,
            index=inventory,
            project=projects[0],
        )


def test_owner_state_inside_project_is_rejected_before_creation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = project / ".owner-state"
    selected = _keys(project, "sample")

    assert _provision(
        tmp_path, project, owner_state, _config(tmp_path), selected, "sample"
    ) != 0
    assert not owner_state.exists()


def test_tmpdir_with_project_symlink_ancestor_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    redirect = project / "redirect"
    redirect.symlink_to(tmp_path, target_is_directory=True)
    project_spelling = redirect / "private-tmp"
    for selector in ("codex", "pinned"):
        binding = config[selector]
        assert isinstance(binding, dict)
        environment = binding["environment"]
        assert isinstance(environment, dict)
        environment["TMPDIR"] = str(project_spelling)

    assert _provision(
        tmp_path,
        project,
        owner_state,
        config,
        _keys(project, "sample"),
        "sample",
    ) != 0
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_replacement_omits_old_roots_and_never_merges_grants(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "first", "first-work")
    _write_recipe(project, "second", "second-work")
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    both = _keys(project, "first", "second")
    first_only = _keys(project, "first")

    assert _provision(
        tmp_path, project, owner_state, config, both, "first", "second"
    ) == 0
    assert _provision(
        tmp_path, project, owner_state, config, first_only, "first"
    ) == 0
    _, snapshot = _snapshot(owner_state)

    assert tuple(_grants(snapshot)) == first_only
    assert snapshot["config_generation"] == 1
    assert snapshot["policy_generation"] == 2

    assert _provision(tmp_path, project, owner_state, config, (), "first") == 0
    _, revoked = _snapshot(owner_state)
    assert revoked["grants"] == []
    assert revoked["config_generation"] == 1
    assert revoked["policy_generation"] == 3


def test_first_snapshot_uses_real_coalesced_inventory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_coalesced_recipe(project)
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    selected = _keys(project, "root")

    recipes = project / ".lockstep" / "recipes"
    index = RuntimeRequirementIndex.for_authorized_closures(
        (preflight_recipe(recipes, "root"),),
        project_identity=str(project.resolve()),
    )
    assert len(index.requirements) == 1
    assert index.requirements[0].uses == (
        ("child.yaml", "shared-work"),
        ("root.recipe.yaml", "shared-work"),
    )

    assert _provision(tmp_path, project, owner_state, config, selected, "root") == 0
    _, snapshot = _snapshot(owner_state)
    grant = _grants(snapshot)[selected[0]]

    assert set(snapshot) == {
        "schema",
        "config_generation",
        "policy_generation",
        "codex",
        "pinned",
        "grants",
    }
    assert snapshot["schema"] == "lockstep.runtime-owner/v1"
    assert snapshot["config_generation"] == 1
    assert snapshot["policy_generation"] == 1
    for selector in ("codex", "pinned"):
        binding = snapshot[selector]
        assert set(binding) == {
            "executable",
            "model",
            "cli_version",
            "permission_profile",
            "codex_home",
            "environment",
            "credential_identity_digest",
            "binding_digest",
            "pinned_permission_profile",
        }
        assert Path(binding["executable"]).is_absolute()
        assert Path(binding["codex_home"]).is_absolute()
        assert len(binding["binding_digest"]) == 64
    assert snapshot["codex"]["credential_identity_digest"] is not None
    assert snapshot["codex"]["pinned_permission_profile"] is None
    assert snapshot["pinned"]["credential_identity_digest"] is None
    assert snapshot["pinned"]["pinned_permission_profile"] == "owner-profile"
    assert grant == {
        "grant_selection_key": selected[0],
        "requirement_digest": requirement_digest(
            grant_selection_key=selected[0],
            runner_binding_digest=snapshot["codex"]["binding_digest"],
            config_generation=1,
        ),
        "authority": "os_user_execution",
        "grant_generation": 1,
        "policy_generation": 1,
        "config_generation": 1,
    }
    snapshot_path = owner_state / "runtime-owner" / "snapshot.json"
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    assert snapshot_path.parent.stat().st_mode & 0o777 == 0o700
    assert owner_state.stat().st_mode & 0o777 == 0o700


def test_binding_change_rekeys_and_reissues_retained_grants(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "sample")

    assert _provision(
        tmp_path, project, owner_state, _config(tmp_path), selected, "sample"
    ) == 0
    _, first = _snapshot(owner_state)
    first_grant = _grants(first)[selected[0]]
    assert _provision(
        tmp_path,
        project,
        owner_state,
        _config(tmp_path, model="changed-model"),
        selected,
        "sample",
    ) == 0
    _, second = _snapshot(owner_state)
    second_grant = _grants(second)[selected[0]]

    assert second["config_generation"] == 2
    assert second["policy_generation"] == 1
    assert second_grant["requirement_digest"] != first_grant["requirement_digest"]
    assert second_grant["grant_generation"] == 2
    assert second_grant["config_generation"] == 2


def test_policy_change_reissues_records_but_retains_predecessor_generation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "first", "first-work")
    _write_recipe(project, "second", "second-work")
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    both = _keys(project, "first", "second")
    first_only = _keys(project, "first")

    assert _provision(
        tmp_path, project, owner_state, config, first_only, "first", "second"
    ) == 0
    assert _provision(
        tmp_path, project, owner_state, config, both, "first", "second"
    ) == 0
    _, snapshot = _snapshot(owner_state)
    grants = _grants(snapshot)

    assert snapshot["policy_generation"] == 2
    assert grants[first_only[0]]["grant_generation"] == 1
    new_key = next(key for key in both if key not in first_only)
    assert grants[new_key]["grant_generation"] == 1
    assert {grant["policy_generation"] for grant in grants.values()} == {2}


def test_binding_and_policy_change_use_stable_key_predecessors(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "codex", "codex-work")
    _write_recipe(project, "pinned", "pinned-work", selector="pinned")
    owner_state = tmp_path / "owner-state"
    both = _keys(project, "codex", "pinned")
    codex_only = _keys(project, "codex")

    assert _provision(
        tmp_path,
        project,
        owner_state,
        _config(tmp_path),
        codex_only,
        "codex",
        "pinned",
    ) == 0
    _, first = _snapshot(owner_state)
    first_codex = _grants(first)[codex_only[0]]

    assert _provision(
        tmp_path,
        project,
        owner_state,
        _config(tmp_path, model="new-model"),
        both,
        "codex",
        "pinned",
    ) == 0
    _, second = _snapshot(owner_state)
    grants = _grants(second)
    pinned_key = next(key for key in both if key not in codex_only)

    assert second["config_generation"] == 2
    assert second["policy_generation"] == 2
    assert grants[codex_only[0]]["grant_generation"] == 2
    assert grants[pinned_key]["grant_generation"] == 1
    assert grants[codex_only[0]]["requirement_digest"] != first_codex[
        "requirement_digest"
    ]
    assert {grant["config_generation"] for grant in grants.values()} == {2}
    assert {grant["policy_generation"] for grant in grants.values()} == {2}


def test_pinned_snapshot_uses_released_runner_binding_digest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "pinned", "pinned-work", selector="pinned")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "pinned")
    config = _config(tmp_path)
    pinned_config = config["pinned"]
    assert isinstance(pinned_config, dict)
    installation = CodexInstallationBinding.capture(
        executable=pinned_config["executable"],
        model=pinned_config["model"],
        cli_version=pinned_config["cli_version"],
        permission_profile=pinned_config["permission_profile"],
        codex_home=pinned_config["codex_home"],
        environment=pinned_config["environment"],
    )

    assert _provision(
        tmp_path,
        project,
        owner_state,
        config,
        selected,
        "pinned",
    ) == 0
    _, snapshot = _snapshot(owner_state)
    pinned = snapshot["pinned"]
    assert pinned["binding_digest"] == _pinned_binding_digest(
        installation.digest,
        pinned["pinned_permission_profile"],
    )
    assert _grants(snapshot)[selected[0]]["requirement_digest"] == requirement_digest(
        grant_selection_key=selected[0],
        runner_binding_digest=pinned["binding_digest"],
        config_generation=1,
    )


def test_poisoned_omitted_predecessor_grant_fails_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "first", "first-work")
    _write_recipe(project, "second", "second-work")
    owner_state = tmp_path / "owner-state"
    config = _config(tmp_path)
    both = _keys(project, "first", "second")
    first_only = _keys(project, "first")
    assert _provision(
        tmp_path, project, owner_state, config, both, "first", "second"
    ) == 0
    path = owner_state / "runtime-owner" / "snapshot.json"
    _, snapshot = _snapshot(owner_state)
    omitted = next(key for key in both if key not in first_only)
    _grants(snapshot)[omitted]["requirement_digest"] = "0" * 64
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    poisoned = path.read_bytes()

    assert _provision(
        tmp_path, project, owner_state, config, first_only, "first"
    ) != 0
    assert path.read_bytes() == poisoned


@pytest.mark.parametrize(
    "poison",
    [
        "missing-key",
        "unknown-key",
        "nonpositive-generation",
        "stale-generation",
        "stale-digest",
        "mode",
        "symlink",
    ],
)
def test_poisoned_existing_snapshot_fails_closed_without_replacement(
    poison: str,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "sample")
    config = _config(tmp_path)
    assert _provision(
        tmp_path, project, owner_state, config, selected, "sample"
    ) == 0
    path = owner_state / "runtime-owner" / "snapshot.json"
    valid_bytes, snapshot = _snapshot(owner_state)
    protected_path = path
    if poison == "missing-key":
        snapshot.pop("policy_generation")
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    elif poison == "unknown-key":
        snapshot["unknown"] = True
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    elif poison == "nonpositive-generation":
        snapshot["config_generation"] = 0
        snapshot["policy_generation"] = 0
        snapshot["grants"][0]["config_generation"] = 0
        snapshot["grants"][0]["policy_generation"] = 0
        snapshot["grants"][0]["requirement_digest"] = requirement_digest(
            grant_selection_key=selected[0],
            runner_binding_digest=snapshot["codex"]["binding_digest"],
            config_generation=0,
        )
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    elif poison == "stale-generation":
        snapshot["grants"][0]["config_generation"] = 0
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    elif poison == "stale-digest":
        snapshot["grants"][0]["requirement_digest"] = "0" * 64
        path.write_text(json.dumps(snapshot), encoding="utf-8")
    elif poison == "mode":
        path.chmod(0o644)
    else:
        target = tmp_path / "snapshot-target.json"
        target.write_bytes(valid_bytes)
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
        protected_path = target
    poisoned_bytes = protected_path.read_bytes()

    assert _provision(
        tmp_path, project, owner_state, config, selected, "sample"
    ) != 0

    assert protected_path.read_bytes() == poisoned_bytes
    if poison == "symlink":
        assert path.is_symlink()
    elif poison == "mode":
        assert path.stat().st_mode & 0o777 == 0o644


def test_two_independent_provisioners_serialize_without_lost_generation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "sample")

    grants = tmp_path / "concurrent-grants.json"
    grants.write_text(json.dumps(selected), encoding="utf-8")
    commands = []
    environment = {**os.environ, "LOCKSTEP_STATE_DIR": str(owner_state)}
    for marker in ("concurrent-a", "concurrent-b"):
        config = tmp_path / f"{marker}.json"
        config.write_text(json.dumps(_config(tmp_path, model=marker)), encoding="utf-8")
        commands.append(
            [sys.executable, "-m", "lockstep", *_argv(project, config, grants, "sample")]
        )

    directory = ensure_owner_directory(owner_state, "runtime-owner")
    with advisory_file_lock(directory / "snapshot.lock"):
        processes = [subprocess.Popen(command, env=environment) for command in commands]
        for process in processes:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
    assert [process.wait(timeout=30) for process in processes] == [0, 0]
    _, snapshot = _snapshot(owner_state)
    grant = _grants(snapshot)[selected[0]]

    assert snapshot["config_generation"] == 2
    assert snapshot["policy_generation"] == 1
    assert grant["grant_generation"] == 2
    assert grant["config_generation"] == 2


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are POSIX-only")
@pytest.mark.parametrize("fifo_input", ["config", "replacement"])
def test_nonregular_owner_input_is_rejected_without_blocking(
    fifo_input: str,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "sample")
    config = tmp_path / "config.json"
    grants = tmp_path / "grants.json"
    config.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
    grants.write_text(json.dumps(selected), encoding="utf-8")
    fifo = config if fifo_input == "config" else grants
    fifo.unlink()
    os.mkfifo(fifo, 0o600)

    assert _subprocess_result(
        _argv(project, config, grants, "sample"), owner_state=owner_state
    ) != 0


@pytest.mark.skipif(os.name != "posix", reason="FIFOs are POSIX-only")
def test_nonregular_existing_snapshot_is_rejected_without_blocking(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "sample")
    config_value = _config(tmp_path)
    assert _provision(
        tmp_path, project, owner_state, config_value, selected, "sample"
    ) == 0
    snapshot = owner_state / "runtime-owner" / "snapshot.json"
    snapshot.unlink()
    os.mkfifo(snapshot, 0o600)
    config = tmp_path / "fifo-snapshot-config.json"
    grants = tmp_path / "fifo-snapshot-grants.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants.write_text(json.dumps(selected), encoding="utf-8")

    assert _subprocess_result(
        _argv(project, config, grants, "sample"), owner_state=owner_state
    ) != 0


@pytest.mark.skipif(os.name != "posix", reason="owner runtime state is POSIX-only")
def test_process_death_releases_the_shared_snapshot_kernel_lock(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _write_recipe(project, "sample", "sample-work")
    owner_state = tmp_path / "owner-state"
    selected = _keys(project, "sample")
    assert _provision(
        tmp_path, project, owner_state, _config(tmp_path), selected, "sample"
    ) == 0
    lock_path = owner_state / "runtime-owner" / "snapshot.lock"
    ready = tmp_path / "lock-ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import signal; "
                "from lockstep.runtime.advisory_lock import advisory_file_lock; "
                f"p=Path({str(lock_path)!r}); r=Path({str(ready)!r}); "
                "ctx=advisory_file_lock(p); ctx.__enter__(); r.touch(); "
                "signal.pause()"
            ),
        ],
        env=os.environ,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        holder.send_signal(signal.SIGKILL)
        assert holder.wait(timeout=10) == -signal.SIGKILL

        assert _provision(
            tmp_path, project, owner_state, _config(tmp_path), selected, "sample"
        ) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
