"""Real public-command setup shared by the bounded Task 12R1b-E tests."""

from __future__ import annotations

import json
import os
import shlex
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from lockstep import cli
from lockstep.recipe.profile import CompilerProvenance
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.effects.owner_provisioning import provision_runtime_snapshot
from lockstep.runtime.service import preflight_recipe


@dataclass(frozen=True, slots=True)
class ProvisionedRuntimeClosure:
    project: Path
    owner_state: Path
    recipe: str
    requirement_index: RuntimeRequirementIndex
    codex_home: Path
    pinned_home: Path
    provider_argv_marker: Path
    provider_environment_marker: Path


@dataclass(slots=True)
class ManagedRestartFifoBarrier:
    """Hold and release the real A15 process at its executable boundary."""

    provisioned: ProvisionedRuntimeClosure
    _result: tuple[tuple[str, ...], tuple[str, ...], bool] | None = None

    @classmethod
    def install(
        cls,
        provisioned: ProvisionedRuntimeClosure,
    ) -> ManagedRestartFifoBarrier:
        os.mkfifo(provisioned.provider_argv_marker)
        return cls(provisioned)

    def release(
        self,
        terminal_path: Path,
        *,
        timeout: float = 10.0,
    ) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
        if self._result is not None:
            return self._result
        descriptor = os.open(
            self.provisioned.provider_argv_marker,
            os.O_RDONLY | os.O_NONBLOCK,
        )
        chunks = []
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    chunks.append(chunk)
                if self.provisioned.provider_environment_marker.is_file():
                    break
                time.sleep(0.02)
            while True:
                try:
                    chunk = os.read(descriptor, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        while not terminal_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        environment = ()
        if self.provisioned.provider_environment_marker.is_file():
            environment = tuple(
                self.provisioned.provider_environment_marker.read_text().splitlines()
            )
        self._result = (
            tuple(b"".join(chunks).decode().splitlines()),
            environment,
            terminal_path.is_file(),
        )
        return self._result


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
                                "bounded_result",
                                "credentials",
                                "network",
                                "sandbox",
                                "workspace",
                            ],
                        },
                        "inputs": {
                            "brief": {"state_key": "brief"},
                            "snapshot": {
                                "runtime_key": "run_start_project_snapshot"
                            },
                        },
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


def write_pinned_verify_recipe(
    project: Path,
    *,
    recipe: str = "pinned-verify",
) -> None:
    """Write one complete real verify closure selecting the pinned runner."""

    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    document = {
        "version": "1.0",
        "name": recipe,
        "state": {
            "command": "dict",
            "request": "dict",
            "result": "dict",
            "lockstep_outcome": "str",
        },
        "nodes": {
            "command": {
                "type": "passthrough",
                "output": {
                    "command": {
                        "schema": "lockstep.pinned-command/v1",
                        "logical_argv": ["python", "-m", "pytest", "-q"],
                        "logical_cwd": ".",
                        "result_source": "exit",
                    }
                },
            },
            "verify": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "verify",
                        "logical_id": "pinned-verify",
                        "runner": {
                            "selector": "pinned",
                            "required_capabilities": [
                                "bounded_result",
                                "sandbox",
                                "workspace",
                            ],
                        },
                        "inputs": {
                            "command": {"state_key": "command"},
                            "snapshot": {
                                "runtime_key": "current_project_snapshot"
                            },
                        },
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
            },
            "done": {
                "type": "passthrough",
                "output": {"lockstep_outcome": "PASS"},
            },
        },
        "edges": [
            {"from": "START", "to": "command"},
            {"from": "command", "to": "verify"},
            {
                "from": "verify",
                "to": "done",
                "condition": "result.outcome == 'PASS'",
            },
            {"from": "done", "to": "END"},
        ],
    }
    (recipes / f"{recipe}.recipe.yaml").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _runtime_config(root: Path) -> dict[str, object]:
    executable = root / "codex"
    provider_argv_marker = root / "provider-argv.txt"
    provider_environment_marker = root / "provider-environment.txt"
    executable.write_text(
        "#!/bin/sh\n"
        + "cat >/dev/null\n"
        + "printf '%s\\n' \"$@\" > "
        + shlex.quote(str(provider_argv_marker))
        + "\n"
        + "printf '%s\\n' \"$CODEX_HOME\" \"$HOME\" > "
        + shlex.quote(str(provider_environment_marker))
        + "\nprintf '%s\\n' "
        + shlex.quote(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                },
                separators=(",", ":"),
            )
        )
        + "\nexit 0\n",
        encoding="utf-8",
    )
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
) -> ProvisionedRuntimeClosure:
    """Create and grant exactly one real protected managed closure."""

    project = root / "project"
    project.mkdir()
    write_managed_recipe(project, recipe=recipe)
    return _provision_written_closure(root, monkeypatch, project, recipe)


def _provision_written_closure(
    root: Path,
    monkeypatch,
    project: Path,
    recipe: str,
) -> ProvisionedRuntimeClosure:
    """Provision one already-written real authorized closure through owner CLI."""

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
    return ProvisionedRuntimeClosure(
        project,
        owner_state,
        recipe,
        index,
        root / "codex-home",
        root / "pinned-home",
        root / "provider-argv.txt",
        root / "provider-environment.txt",
    )


def provision_pinned_verify_closure(
    root: Path,
    monkeypatch,
    *,
    recipe: str = "pinned-verify",
) -> ProvisionedRuntimeClosure:
    """Create and grant exactly one real protected pinned verify closure."""

    project = root / "project"
    project.mkdir()
    write_pinned_verify_recipe(project, recipe=recipe)
    provisioned = _provision_written_closure(root, monkeypatch, project, recipe)
    requirement = provisioned.requirement_index.requirements[0]
    assert requirement.runner_selector == "pinned"
    return provisioned


def provision_compiled_managed_closure(
    root: Path,
    monkeypatch,
    *,
    project: Path,
    recipe: str,
    compiler_provenance: CompilerProvenance,
) -> ProvisionedRuntimeClosure:
    """Provision one compiler-authorized managed closure through owner storage."""

    recipes = project / ".lockstep" / "recipes"
    index = RuntimeRequirementIndex.for_authorized_closures(
        (
            preflight_recipe(
                recipes,
                recipe,
                compiler_provenance=compiler_provenance,
            ),
        ),
        project_identity=str(project.resolve()),
    )
    assert len(index.requirements) == 1
    selection_keys = tuple(
        requirement.grant_selection_key for requirement in index.requirements
    )
    config = _runtime_config(root)
    codex = config["codex"]
    pinned = config["pinned"]
    assert isinstance(codex, dict)
    assert isinstance(pinned, dict)
    owner_state = root / "owner-state"
    provision_runtime_snapshot(
        state_dir=owner_state,
        codex=codex,
        pinned=pinned,
        replacement_keys=selection_keys,
        index=index,
        project=project,
    )
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    return ProvisionedRuntimeClosure(
        project,
        owner_state,
        recipe,
        index,
        root / "codex-home",
        root / "pinned-home",
        root / "provider-argv.txt",
        root / "provider-environment.txt",
    )
