"""Task 12R0 Gate A-schema: independently missing public contracts on b794."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from lockstep.authoring import json_text
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.engine import Engine, LockstepError

from lockstep import cli


def _write_empty_runtime_recipe(project: Path) -> None:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    (recipes / "sample.recipe.yaml").write_text(
        json.dumps(
            {
                "version": "1.0",
                "name": "sample",
                "state": {},
                "nodes": {"done": {"type": "passthrough"}},
                "edges": [
                    {"from": "START", "to": "done"},
                    {"from": "done", "to": "END"},
                ],
            }
        ),
        encoding="utf-8",
    )


def _authorized_managed_recipe(
    recipes: Path,
    *,
    name: str,
    logical_id: str,
):
    document = {
        "version": "1.0",
        "name": name,
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "managed",
                        "logical_id": logical_id,
                        "runner": {
                            "selector": "codex",
                            "required_capabilities": [
                                "workspace",
                                "bounded_result",
                            ],
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
        },
        "edges": [
            {"from": "START", "to": "work"},
            {"from": "work", "to": "END"},
        ],
    }
    path = recipes / f"{name}.recipe.yaml"
    path.write_text(json.dumps(document), encoding="utf-8")
    return StrictRecipeIngress(recipes).inspect(path.name).authorize(
        RecipeAuthorityPolicy()
    )


def _provision_config_shape() -> dict[str, object]:
    common = {
        "executable": "/codex",
        "model": "model",
        "cli_version": "version",
        "permission_profile": {
            "sandbox": "workspace-write",
            "approval": "never",
        },
        "codex_home": "/codex-home",
        "environment": {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": "/private-tmp",
        },
    }
    return {
        "schema": "lockstep.runtime-provision-config/v1",
        "codex": dict(common),
        "pinned": {**common, "pinned_permission_profile": "owner-profile"},
    }


def _valid_provision_config(tmp_path: Path) -> dict[str, object]:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "auth.json").chmod(0o600)
    pinned_home = tmp_path / "pinned-home"
    pinned_home.mkdir(mode=0o700)
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir(mode=0o700)
    config = _provision_config_shape()
    codex = config["codex"]
    pinned = config["pinned"]
    assert isinstance(codex, dict) and isinstance(pinned, dict)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(private_tmp),
    }
    codex.update(
        executable=str(executable),
        codex_home=str(codex_home),
        environment=environment,
    )
    pinned.update(
        executable=str(executable),
        codex_home=str(pinned_home),
        environment=environment,
    )
    return config


def _provision_argv(project: Path, config: Path, grants: Path) -> list[str]:
    return [
        "owner",
        "provision-runtime",
        "--config",
        str(config),
        "--project",
        str(project),
        "--recipe",
        "sample",
        "--replace-grants",
        str(grants),
    ]


def test_engine_command_construction_is_write_and_recovery_inert(tmp_path) -> None:
    state = tmp_path / "owner-state"

    command = Engine.command(state, tmp_path / "recipes")
    command.close()

    assert not state.exists()


def test_managed_start_fails_at_execution_policy_boundary_before_owner_write(
    tmp_path,
) -> None:
    golden = Path(__file__).parents[1] / "workflow" / "golden"
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    source = StrictRecipeIngress(golden).inspect("control-flow.recipe.yaml")
    document = json.loads(source.files[0].bytes)
    document.pop("x-lockstep-generated")
    recipe_path = recipes / "control-flow.recipe.yaml"
    recipe_path.write_text(json.dumps(document), encoding="utf-8")
    authorized = StrictRecipeIngress(recipes).inspect(recipe_path.name).authorize(
        RecipeAuthorityPolicy()
    )
    state = tmp_path / "owner-state"
    project = tmp_path / "project"
    project.mkdir()
    command = Engine.command(state, recipes)

    try:
        with pytest.raises(
            LockstepError,
            match="^runtime execution policy is unavailable$",
        ):
            command.start_authorized("control-flow", authorized, {}, str(project))
    finally:
        command.close()

    assert not state.exists()


def test_runtime_requirement_rejects_key_for_a_different_stable_tuple() -> None:
    module = importlib.import_module("lockstep.runtime.effects.owner_policy")
    stable = {
        "project_identity": "project-a",
        "definition_digest": "definition",
        "protected_descriptor_digest": "descriptor",
        "runner_selector": "codex",
        "required_capabilities": ("cap.alpha",),
        "required_authorities": ("authority.execute",),
    }
    selection_key = module.grant_selection_key(**stable)

    with pytest.raises(ValueError):
        module.RuntimeRequirement(
            grant_selection_key=selection_key,
            **{**stable, "project_identity": "project-b"},
            uses=(),
        )


def test_requirement_index_derives_one_real_pinned_descriptor() -> None:
    module = importlib.import_module("lockstep.runtime.effects.owner_policy")
    recipes = Path(__file__).parents[1] / "workflow" / "golden"
    authorized = StrictRecipeIngress(recipes).inspect(
        "control-flow.recipe.yaml"
    ).authorize(RecipeAuthorityPolicy())

    index = module.RuntimeRequirementIndex.for_authorized_closure(
        authorized,
        project_identity="/project",
    )

    assert index.project_identity == "/project"
    assert index.requirements == (
        module.RuntimeRequirement(
            grant_selection_key=(
                "b4702f9aa9c5c880b1a7c2c42e14959b8683256a0cc91207767b135910eb28a4"
            ),
            project_identity="/project",
            definition_digest=(
                "d014747dcad956e7f8df67f2d2c63ada477aad7f760916c19dc4bb062353fdd2"
            ),
            protected_descriptor_digest=(
                "b887484bad207296f2903586eef5a66e068f861971662b71592b9ae99ba3788c"
            ),
            runner_selector="pinned",
            required_capabilities=("bounded_result", "sandbox", "workspace"),
            required_authorities=("os_user_execution",),
            uses=(("control-flow.recipe.yaml", "focused"),),
        ),
    )


def test_requirement_index_coalesces_identical_facts_across_real_dag(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("lockstep.runtime.effects.owner_policy")
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "managed",
        "logical_id": "same-effect",
        "runner": {
            "selector": "codex",
            "required_capabilities": ["workspace", "bounded_result"],
        },
        "inputs": {},
        "writes": [],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }

    def document(name: str, *, child: bool) -> dict:
        nodes = {
            "work": {
                "type": "interrupt",
                "message": {"lockstep_effect": descriptor},
                "state_key": "request",
                "resume_key": "result",
                "idempotent": False,
            }
        }
        edges = [{"from": "START", "to": "work"}]
        if child:
            nodes["child"] = {
                "type": "subgraph",
                "graph": "child.recipe.yaml",
                "mode": "direct",
            }
            edges.extend(
                [
                    {"from": "work", "to": "child"},
                    {"from": "child", "to": "END"},
                ]
            )
        else:
            edges.append({"from": "work", "to": "END"})
        return {
            "version": "1.0",
            "name": name,
            "state": {"request": "dict", "result": "dict"},
            "nodes": nodes,
            "edges": edges,
        }

    (tmp_path / "root.recipe.yaml").write_text(
        json.dumps(document("root", child=True)), encoding="utf-8"
    )
    (tmp_path / "child.recipe.yaml").write_text(
        json.dumps(document("child", child=False)), encoding="utf-8"
    )
    authorized = StrictRecipeIngress(tmp_path).inspect(
        "root.recipe.yaml"
    ).authorize(RecipeAuthorityPolicy())

    index = module.RuntimeRequirementIndex.for_authorized_closure(
        authorized,
        project_identity="/project",
    )

    assert len(index.requirements) == 1
    assert index.requirements[0].uses == (
        ("child.recipe.yaml", "same-effect"),
        ("root.recipe.yaml", "same-effect"),
    )


def test_requirement_index_union_is_canonical_across_distinct_root_order(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("lockstep.runtime.effects.owner_policy")
    first = _authorized_managed_recipe(
        tmp_path, name="first", logical_id="first-effect"
    )
    second = _authorized_managed_recipe(
        tmp_path, name="second", logical_id="second-effect"
    )

    forward = module.RuntimeRequirementIndex.for_authorized_closures(
        (first, second), project_identity="/project"
    )
    reverse = module.RuntimeRequirementIndex.for_authorized_closures(
        (second, first), project_identity="/project"
    )

    assert forward == reverse
    assert len(forward.requirements) == 2


def test_owner_provision_runtime_rejects_config_symlink_before_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_target = tmp_path / "config-target.json"
    config_target.write_text(
        json.dumps(_valid_provision_config(tmp_path)),
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.symlink_to(config_target)
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(
        [
            "owner",
            "provision-runtime",
            "--config",
            str(config),
            "--project",
            str(project),
            "--recipe",
            "sample",
            "--replace-grants",
            str(grants),
        ]
    )

    captured = capsys.readouterr()
    assert result != 0
    assert "regular non-symlink" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_provision_runtime_rejects_oversize_config_before_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.json"
    config.write_bytes(b"x" * (64 * 1024 + 1))
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(
        [
            "owner",
            "provision-runtime",
            "--config",
            str(config),
            "--project",
            str(project),
            "--recipe",
            "sample",
            "--replace-grants",
            str(grants),
        ]
    )

    captured = capsys.readouterr()
    assert result != 0
    assert "exceeds 65536 bytes" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


@pytest.mark.parametrize("member", ["codex", "pinned"])
def test_owner_provision_runtime_rejects_binding_home_symlink(
    member: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_value = _valid_provision_config(tmp_path)
    binding = config_value[member]
    assert isinstance(binding, dict)
    target = Path(str(binding["codex_home"]))
    linked_home = tmp_path / f"{member}-linked-home"
    linked_home.symlink_to(target, target_is_directory=True)
    binding["codex_home"] = str(linked_home)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(
        [
            "owner",
            "provision-runtime",
            "--config",
            str(config),
            "--project",
            str(project),
            "--recipe",
            "sample",
            "--replace-grants",
            str(grants),
        ]
    )

    captured = capsys.readouterr()
    assert result != 0
    assert "symlink" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


@pytest.mark.parametrize("member", ["codex", "pinned"])
def test_owner_provision_runtime_rejects_shared_home_mode(
    member: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_value = _valid_provision_config(tmp_path)
    binding = config_value[member]
    assert isinstance(binding, dict)
    Path(str(binding["codex_home"])).chmod(0o704)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "owner-only" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


@pytest.mark.parametrize("member", ["codex", "pinned"])
def test_owner_provision_runtime_enforces_distinct_credential_roles(
    member: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_value = _valid_provision_config(tmp_path)
    binding = config_value[member]
    assert isinstance(binding, dict)
    auth = Path(str(binding["codex_home"])) / "auth.json"
    if member == "codex":
        auth.unlink()
    else:
        auth.write_text("{}", encoding="utf-8")
        auth.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "credential" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_provision_runtime_rejects_shared_auth_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_value = _valid_provision_config(tmp_path)
    codex = config_value["codex"]
    assert isinstance(codex, dict)
    (Path(str(codex["codex_home"])) / "auth.json").chmod(0o604)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "owner-only" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_provision_runtime_rejects_symlink_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_value = _valid_provision_config(tmp_path)
    codex = config_value["codex"]
    pinned = config_value["pinned"]
    assert isinstance(codex, dict) and isinstance(pinned, dict)
    environment = codex["environment"]
    assert isinstance(environment, dict)
    target = Path(str(environment["TMPDIR"]))
    linked_tmp = tmp_path / "linked-tmp"
    linked_tmp.symlink_to(target, target_is_directory=True)
    for binding in (codex, pinned):
        values = binding["environment"]
        assert isinstance(values, dict)
        values["TMPDIR"] = str(linked_tmp)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "TMPDIR" in captured.err and "symlink" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_provision_runtime_rejects_shared_tmpdir_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config_value = _valid_provision_config(tmp_path)
    codex = config_value["codex"]
    assert isinstance(codex, dict)
    environment = codex["environment"]
    assert isinstance(environment, dict)
    Path(str(environment["TMPDIR"])).chmod(0o704)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "TMPDIR" in captured.err and "owner-only" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_provision_runtime_rejects_tmpdir_inside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    project_tmp = project / ".private-tmp"
    project_tmp.mkdir(mode=0o700)
    config_value = _valid_provision_config(tmp_path)
    for member in ("codex", "pinned"):
        binding = config_value[member]
        assert isinstance(binding, dict)
        environment = binding["environment"]
        assert isinstance(environment, dict)
        environment["TMPDIR"] = str(project_tmp)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(config_value), encoding="utf-8")
    grants = tmp_path / "grants.json"
    grants.write_text("[]", encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "TMPDIR" in captured.err and "outside project" in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "oversize"])
def test_owner_provision_runtime_bounds_replacement_file_before_decoding(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(_valid_provision_config(tmp_path)), encoding="utf-8"
    )
    grants = tmp_path / "grants.json"
    if kind == "symlink":
        target = tmp_path / "grants-target.json"
        target.write_text("[]", encoding="utf-8")
        grants.symlink_to(target)
    else:
        grants.write_bytes(b"x" * (512 * 1024 + 1))
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    expected = "regular non-symlink" if kind == "symlink" else "exceeds 524288 bytes"
    assert expected in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_provision_runtime_rejects_key_outside_static_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    _write_empty_runtime_recipe(project)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(_valid_provision_config(tmp_path)), encoding="utf-8"
    )
    grants = tmp_path / "grants.json"
    grants.write_text(json.dumps(["0" * 64]), encoding="utf-8")
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    result = cli.main(_provision_argv(project, config, grants))

    captured = capsys.readouterr()
    assert result != 0
    assert "outside the static runtime inventory" in captured.err
    assert "policy is unavailable" not in captured.err
    assert not (owner_state / "runtime-owner" / "snapshot.json").exists()


def test_owner_cli_lists_real_static_requirements_without_owner_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module("lockstep.runtime.effects.owner_policy")
    project = tmp_path / "project"
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    golden = Path(__file__).parents[1] / "workflow" / "golden"
    source = StrictRecipeIngress(golden).inspect("control-flow.recipe.yaml")
    document = json.loads(source.files[0].bytes)
    document.pop("x-lockstep-generated")
    document["name"] = "sample"
    recipe_path = recipes / "sample.recipe.yaml"
    recipe_path.write_text(json.dumps(document), encoding="utf-8")
    candidate = StrictRecipeIngress(recipes).inspect(recipe_path.name)
    project_identity = str(project.resolve())
    protected_digest = (
        "b887484bad207296f2903586eef5a66e068f861971662b71592b9ae99ba3788c"
    )
    selection_key = module.grant_selection_key(
        project_identity=project_identity,
        definition_digest=candidate.definition_sha256,
        protected_descriptor_digest=protected_digest,
        runner_selector="pinned",
        required_capabilities=("bounded_result", "sandbox", "workspace"),
        required_authorities=("os_user_execution",),
    )
    owner_state = tmp_path / "owner-state"
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))

    assert cli.main(
        [
            "owner",
            "list-runtime-requirements",
            "--project",
            str(project),
            "--recipe",
            "sample",
            "--recipe",
            "sample",
        ]
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == json_text(
        {
            "schema": "lockstep.runtime-requirements/v1",
            "project_identity": project_identity,
            "requirements": [
                {
                    "grant_selection_key": selection_key,
                    "definition_digest": candidate.definition_sha256,
                    "protected_descriptor_digest": protected_digest,
                    "runner_selector": "pinned",
                    "required_capabilities": [
                        "bounded_result",
                        "sandbox",
                        "workspace",
                    ],
                    "required_authorities": ["os_user_execution"],
                    "uses": [
                        {
                            "logical_file": "sample.recipe.yaml",
                            "logical_id": "focused",
                        }
                    ],
                }
            ],
        }
    )
    assert not owner_state.exists()
