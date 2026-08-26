"""Real public-command setup shared by the bounded Task 12R1b-E tests."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep import cli
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.service import preflight_recipe


@dataclass(frozen=True, slots=True)
class ProvisionedManagedClosure:
    project: Path
    owner_state: Path
    recipe: str
    requirement_index: RuntimeRequirementIndex


def write_managed_recipe(project: Path, *, recipe: str = "managed-work") -> None:
    """Write one complete real protected Codex recipe through public ingress."""

    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    document = {
        "version": "1.0",
        "name": recipe,
        "state": {"brief": "str", "request": "dict", "result": "dict"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "managed",
                        "logical_id": "managed-work",
                        "runner": {
                            "selector": "codex",
                            "required_capabilities": [
                                "workspace",
                                "bounded_result",
                            ],
                        },
                        "inputs": {"brief": {"state_key": "brief"}},
                        "writes": [],
                        "artifacts": [],
                        "deadline_seconds": 120,
                        "scope_state_keys": [],
                        "result_schema": "lockstep.effect-result/v1",
                    }
                },
                "state_key": "request",
                "resume_key": "result",
                "idempotent": False,
            }
        },
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "END"},
        ],
    }
    (recipes / f"{recipe}.recipe.yaml").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _runtime_config(root: Path) -> dict[str, object]:
    executable = root / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    codex_home = root / "codex-home"
    codex_home.mkdir(mode=0o700)
    auth = codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    pinned_home = root / "pinned-home"
    pinned_home.mkdir(mode=0o700)
    private_tmp = root / "private-tmp"
    private_tmp.mkdir(mode=0o700)
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(private_tmp),
    }
    common = {
        "executable": str(executable),
        "model": "task12-r1be-test-model",
        "cli_version": "task12-r1be-test-version",
        "permission_profile": {"sandbox": "workspace-write", "approval": "never"},
        "environment": environment,
    }
    return {
        "schema": "lockstep.runtime-provision-config/v1",
        "codex": {**common, "codex_home": str(codex_home)},
        "pinned": {
            **common,
            "codex_home": str(pinned_home),
            "pinned_permission_profile": "task12-pinned-profile",
        },
    }


def provision_managed_closure(
    root: Path,
    monkeypatch,
    *,
    recipe: str = "managed-work",
) -> ProvisionedManagedClosure:
    """Create and grant exactly one real protected managed closure."""

    project = root / "project"
    project.mkdir()
    write_managed_recipe(project, recipe=recipe)
    recipes = project / ".lockstep" / "recipes"
    index = RuntimeRequirementIndex.for_authorized_closures(
        (preflight_recipe(recipes, recipe),),
        project_identity=str(project.resolve()),
    )
    assert len(index.requirements) == 1
    selection_keys = tuple(
        requirement.grant_selection_key for requirement in index.requirements
    )
    config_path = root / "runtime-config.json"
    grants_path = root / "runtime-grants.json"
    config_path.write_text(json.dumps(_runtime_config(root)), encoding="utf-8")
    grants_path.write_text(json.dumps(selection_keys), encoding="utf-8")
    owner_state = root / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    assert cli.main(
        [
            "owner",
            "provision-runtime",
            "--config",
            str(config_path),
            "--project",
            str(project),
            "--recipe",
            recipe,
            "--replace-grants",
            str(grants_path),
        ]
    ) == 0
    return ProvisionedManagedClosure(project, owner_state, recipe, index)
