"""Focused real-path harness for R1b-A1 ordering races."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import stat
import threading

from lockstep import cli
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.engine import Engine
from lockstep.runtime.read_resources import RuntimeReadResources
from lockstep.runtime.service import preflight_recipe


def _effect_recipe(name: str, *, kind: str) -> dict[str, object]:
    managed = kind == "managed"
    return {
        "version": "1.0",
        "name": name,
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": kind,
                        "logical_id": f"{name}-work",
                        "runner": (
                            {
                                "selector": "codex",
                                "required_capabilities": [
                                    "workspace",
                                    "bounded_result",
                                ],
                            }
                            if managed
                            else None
                        ),
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


def _write_recipes(project: Path, *, include_prior: bool) -> Path:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    documents = {"target": _effect_recipe("target", kind="managed")}
    if include_prior:
        documents["prior"] = _effect_recipe("prior", kind="manual")
    for name, document in documents.items():
        (recipes / f"{name}.recipe.yaml").write_text(
            json.dumps(document), encoding="utf-8"
        )
    return recipes


def _runtime_config(tmp_path: Path, provider_marker: Path) -> dict[str, object]:
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\nprintf invoked > "
        + shlex.quote(str(provider_marker))
        + "\nexit 0\n",
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


@dataclass(frozen=True)
class A1Harness:
    tmp_path: Path
    project: Path
    owner_state: Path
    recipes: Path
    config: dict[str, object]
    granted: tuple[str, ...]
    provider_marker: Path

    @classmethod
    def granted_runtime(
        cls,
        tmp_path: Path,
        monkeypatch,
        *,
        include_prior: bool = False,
    ) -> "A1Harness":
        project = tmp_path / "project"
        recipes = _write_recipes(project, include_prior=include_prior)
        owner_state = tmp_path / "owner-state"
        provider_marker = tmp_path / "provider-invoked"
        config = _runtime_config(tmp_path, provider_marker)
        index = RuntimeRequirementIndex.for_authorized_closures(
            (preflight_recipe(recipes, "target"),),
            project_identity=str(project.resolve()),
        )
        granted = tuple(item.grant_selection_key for item in index.requirements)
        assert len(granted) == 1
        monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
        harness = cls(
            tmp_path,
            project,
            owner_state,
            recipes,
            config,
            granted,
            provider_marker,
        )
        assert harness.provision(granted, suffix="grant") == 0
        return harness

    @property
    def project_identity(self) -> str:
        return str(self.project.resolve())

    def command(self):
        return Engine.command(self.owner_state, self.recipes)

    def provision(self, replacement: tuple[str, ...], *, suffix: str) -> int:
        config_path = self.tmp_path / f"runtime-config-{suffix}.json"
        grants_path = self.tmp_path / f"runtime-grants-{suffix}.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        grants_path.write_text(json.dumps(replacement), encoding="utf-8")
        return cli.main(
            [
                "owner",
                "provision-runtime",
                "--config",
                str(config_path),
                "--project",
                str(self.project),
                "--recipe",
                "target",
                "--replace-grants",
                str(grants_path),
            ]
        )

    def run_ids(self) -> tuple[str, ...]:
        resources = RuntimeReadResources(self.owner_state)
        return tuple(
            binding.public_run_id
            for binding in resources.bindings_for_project(self.project_identity)
        )


@dataclass(frozen=True)
class ThreadCall:
    outcome: list[object]
    finished: threading.Event
    thread: threading.Thread
    start_attempted: threading.Event

    @classmethod
    def create(cls, name: str, call: Callable[[], object]) -> "ThreadCall":
        outcome: list[object] = []
        finished = threading.Event()
        start_attempted = threading.Event()

        def invoke() -> None:
            try:
                outcome.append(call())
            except BaseException as exc:
                outcome.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=invoke, name=name)
        return cls(outcome, finished, thread, start_attempted)

    @property
    def name(self) -> str:
        return self.thread.name

    def start(self) -> None:
        self.start_attempted.set()
        self.thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        if not self.start_attempted.is_set() or self.thread.ident is None:
            return not self.thread.is_alive()
        finished = self.finished.wait(timeout)
        self.thread.join(timeout=1.0)
        return finished and not self.thread.is_alive()


@dataclass
class A1ConcurrentCleanup:
    """Own every A1 thread and barrier until its command service is closed."""

    close_service: Callable[[], None]
    release_events: tuple[threading.Event, ...]
    calls: list[ThreadCall] = field(default_factory=list)
    threads_stopped: bool = False

    def __enter__(self) -> "A1ConcurrentCleanup":
        return self

    def launch(self, name: str, call: Callable[[], object]) -> ThreadCall:
        registered = ThreadCall.create(name, call)
        self.calls.append(registered)
        registered.start()
        return registered

    def __exit__(self, _kind, original: BaseException | None, _traceback) -> bool:
        cleanup_errors: list[BaseException] = []
        stopped: list[bool] = []
        try:
            for release in self.release_events:
                try:
                    release.set()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            for call in self.calls:
                try:
                    call_stopped = call.stop()
                    stopped.append(call_stopped)
                    if not call_stopped:
                        cleanup_errors.append(
                            RuntimeError(f"A1 thread {call.name!r} did not stop")
                        )
                except BaseException as exc:
                    stopped.append(False)
                    cleanup_errors.append(exc)
        finally:
            try:
                self.close_service()
            except BaseException as exc:
                cleanup_errors.append(exc)

        self.threads_stopped = all(stopped)
        cleanup_failed = not self.threads_stopped or bool(cleanup_errors)
        if cleanup_failed and original is not None:
            original.add_note(self._failure_note(cleanup_errors))
        elif cleanup_errors:
            raise BaseExceptionGroup("A1 concurrent cleanup failed", cleanup_errors)
        return False

    def _failure_note(self, errors: list[BaseException]) -> str:
        details: list[str] = []
        if not self.threads_stopped:
            details.append("not every started A1 thread stopped")
        details.extend(f"{type(exc).__name__}: {exc}" for exc in errors)
        return "A1 cleanup assertion failed after all actions: " + "; ".join(details)


def owner_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str], ...]:
    """Capture all durable owner facts without following links."""

    entries: list[tuple[str, str, int, bytes | str]] = []

    def visit(path: Path, relative: str) -> None:
        metadata = os.lstat(path)
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode, ""))
            with os.scandir(path) as children:
                for child in sorted(children, key=lambda item: item.name):
                    child_relative = (
                        child.name if relative == "." else f"{relative}/{child.name}"
                    )
                    visit(Path(child.path), child_relative)
        elif stat.S_ISREG(metadata.st_mode):
            entries.append((relative, "regular", mode, path.read_bytes()))
        elif stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, os.readlink(path)))
        else:
            entries.append((relative, "other", mode, ""))

    visit(root, ".")
    return tuple(entries)
