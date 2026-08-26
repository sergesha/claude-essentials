"""R1b-A1: owner-policy drift invalidates a completed static preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat
import threading

import pytest

from lockstep import cli
from lockstep.runtime import service as service_module
from lockstep.runtime import start_service as start_service_module
from lockstep.runtime.effects.owner_policy import RuntimeRequirementIndex
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.engine import Engine
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.service import preflight_recipe


def _write_managed_recipe(project: Path) -> None:
    recipes = project / ".lockstep" / "recipes"
    recipes.mkdir(parents=True)
    document = {
        "version": "1.0",
        "name": "target",
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "managed",
                        "logical_id": "target-work",
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
    (recipes / "target.recipe.yaml").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _write_manual_recipe(project: Path) -> None:
    document = {
        "version": "1.0",
        "name": "prior",
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "work": {
                "type": "interrupt",
                "message": {
                    "lockstep_effect": {
                        "schema": "lockstep.effect/v1",
                        "kind": "manual",
                        "logical_id": "prior-work",
                        "runner": None,
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
    recipes = project / ".lockstep" / "recipes"
    (recipes / "prior.recipe.yaml").write_text(
        json.dumps(document), encoding="utf-8"
    )


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


def _provision(
    *,
    tmp_path: Path,
    project: Path,
    config: dict[str, object],
    replacement: tuple[str, ...],
    suffix: str,
) -> int:
    config_path = tmp_path / f"runtime-config-{suffix}.json"
    grants_path = tmp_path / f"runtime-grants-{suffix}.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    grants_path.write_text(json.dumps(replacement), encoding="utf-8")
    return cli.main(
        [
            "owner",
            "provision-runtime",
            "--config",
            str(config_path),
            "--project",
            str(project),
            "--recipe",
            "target",
            "--replace-grants",
            str(grants_path),
        ]
    )


def _owner_tree(root: Path) -> tuple[tuple[str, str, int, bytes | str], ...]:
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


def test_supported_revocation_after_real_preflight_is_write_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Currentness must not allow stale admission facts to be persisted."""

    project = tmp_path / "project"
    _write_managed_recipe(project)
    owner_state = tmp_path / "owner-state"
    provider_marker = tmp_path / "provider-invoked"
    config = _runtime_config(tmp_path, provider_marker)
    recipes = project / ".lockstep" / "recipes"
    requirement_index = RuntimeRequirementIndex.for_authorized_closures(
        (preflight_recipe(recipes, "target"),),
        project_identity=str(project.resolve()),
    )
    granted = tuple(
        item.grant_selection_key for item in requirement_index.requirements
    )
    assert len(granted) == 1
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    assert _provision(
        tmp_path=tmp_path,
        project=project,
        config=config,
        replacement=granted,
        suffix="grant",
    ) == 0
    granted_digest, granted_snapshot = open_runtime_snapshot(owner_state)
    assert tuple(
        grant.grant_selection_key for grant in granted_snapshot.grants
    ) == granted

    original_plan = service_module.plan_authorized_start
    preflight_finished = threading.Event()
    release_start = threading.Event()
    start_finished = threading.Event()
    outcome: list[object] = []

    def barrier_after_real_preflight(**kwargs):
        plan = original_plan(**kwargs)
        assert plan.runtime_admission is not None
        preflight_finished.set()
        if not release_start.wait(10.0):
            raise AssertionError("timed out waiting to release admitted start")
        return plan

    monkeypatch.setattr(
        service_module, "plan_authorized_start", barrier_after_real_preflight
    )
    service = Engine.command(owner_state, recipes)

    def start() -> None:
        try:
            outcome.append(service.start("target", {}, str(project)))
        except BaseException as exc:
            outcome.append(exc)
        finally:
            start_finished.set()

    thread = threading.Thread(target=start, name="lockstep-a1-start")
    thread.start()
    start_stopped = False
    try:
        assert preflight_finished.wait(10.0), "real static preflight did not finish"
        assert _provision(
            tmp_path=tmp_path,
            project=project,
            config=config,
            replacement=(),
            suffix="revoke",
        ) == 0
        revoked_digest, revoked_snapshot = open_runtime_snapshot(owner_state)
        assert revoked_digest != granted_digest
        assert (
            revoked_snapshot.config_generation
            == granted_snapshot.config_generation
        )
        assert (
            revoked_snapshot.policy_generation
            == granted_snapshot.policy_generation + 1
        )
        assert revoked_snapshot.codex == granted_snapshot.codex
        assert revoked_snapshot.pinned == granted_snapshot.pinned
        assert revoked_snapshot.grants == ()
        expected_after_supported_drift = _owner_tree(owner_state)
    finally:
        release_start.set()
        start_stopped = start_finished.wait(10.0)
        thread.join(timeout=1.0)
        try:
            service.close()
        finally:
            if thread.is_alive():
                release_start.set()
                thread.join(timeout=10.0)
    assert start_stopped, "start did not finish after release"

    # Exact tree identity covers every start-side durable class: runtime DB and
    # catalog binding, blobs, recipe bundle/materialization, project snapshot,
    # runtime input/watch, checkpoint, effects/observations/events, launch/grant
    # audit, delivery, and continuation.  The provisioning snapshot change is
    # the sole expected write.  The marker separately covers provider execution.
    observed = {
        "rejected": len(outcome) == 1 and isinstance(outcome[0], LockstepError),
        "worker_stopped": not thread.is_alive(),
        "owner_tree_unchanged_after_drift": (
            _owner_tree(owner_state) == expected_after_supported_drift
        ),
        "runtime_database_absent": not (owner_state / "runtime.sqlite").exists(),
        "native_checkpoint_absent": not (
            owner_state / "checkpoints" / "native.sqlite"
        ).exists(),
        "provider_marker_absent": not provider_marker.exists(),
    }
    assert observed == {
        "rejected": True,
        "worker_stopped": True,
        "owner_tree_unchanged_after_drift": True,
        "runtime_database_absent": True,
        "native_checkpoint_absent": True,
        "provider_marker_absent": True,
    }


def test_admission_first_holds_snapshot_lock_until_durable_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the owner lock before the park must unblock revocation early."""

    project = tmp_path / "project"
    _write_managed_recipe(project)
    owner_state = tmp_path / "owner-state"
    config = _runtime_config(tmp_path, tmp_path / "provider-invoked")
    recipes = project / ".lockstep" / "recipes"
    index = RuntimeRequirementIndex.for_authorized_closures(
        (preflight_recipe(recipes, "target"),),
        project_identity=str(project.resolve()),
    )
    granted = tuple(item.grant_selection_key for item in index.requirements)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    assert _provision(
        tmp_path=tmp_path,
        project=project,
        config=config,
        replacement=granted,
        suffix="admission-first-grant",
    ) == 0

    park_entered = threading.Event()
    release_park = threading.Event()
    start_finished = threading.Event()
    provision_finished = threading.Event()
    start_outcome: list[object] = []
    provision_outcome: list[object] = []
    original_park = start_service_module.AuthorizedStartService._admit_and_park

    def blocked_park(self, *args, **kwargs):
        park_entered.set()
        if not release_park.wait(10.0):
            raise AssertionError("timed out waiting to persist admitted park")
        return original_park(self, *args, **kwargs)

    monkeypatch.setattr(
        start_service_module.AuthorizedStartService,
        "_admit_and_park",
        blocked_park,
    )
    service = Engine.command(owner_state, recipes)

    def start() -> None:
        try:
            start_outcome.append(service.start("target", {}, str(project)))
        except BaseException as exc:
            start_outcome.append(exc)
        finally:
            start_finished.set()

    def revoke() -> None:
        try:
            provision_outcome.append(
                _provision(
                    tmp_path=tmp_path,
                    project=project,
                    config=config,
                    replacement=(),
                    suffix="admission-first-revoke",
                )
            )
        except BaseException as exc:
            provision_outcome.append(exc)
        finally:
            provision_finished.set()

    start_thread = threading.Thread(target=start, name="lockstep-a1-admit-first")
    provision_thread = threading.Thread(
        target=revoke, name="lockstep-a1-admit-first-provision"
    )
    start_thread.start()
    try:
        assert park_entered.wait(10.0), "start never reached the durable park"
        provision_thread.start()
        provisioning_blocked_before_park = not provision_finished.wait(0.5)
    finally:
        release_park.set()
        start_finished.wait(10.0)
        provision_finished.wait(10.0)
        start_thread.join(timeout=1.0)
        provision_thread.join(timeout=1.0)
        service.close()

    observed = {
        "provisioning_blocked_before_park": provisioning_blocked_before_park,
        "start_finished": start_finished.is_set() and not start_thread.is_alive(),
        "provision_finished": (
            provision_finished.is_set() and not provision_thread.is_alive()
        ),
        "start_parked": (
            len(start_outcome) == 1
            and isinstance(start_outcome[0], dict)
            and start_outcome[0].get("status") == "starting"
        ),
        "supported_revoke_succeeded": provision_outcome == [0],
    }
    assert observed == {
        "provisioning_blocked_before_park": True,
        "start_finished": True,
        "provision_finished": True,
        "start_parked": True,
        "supported_revoke_succeeded": True,
    }


def test_cold_recovery_runs_after_park_without_holding_snapshot_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping the owner lock through unrelated recovery must block revoke."""

    project = tmp_path / "project"
    _write_managed_recipe(project)
    _write_manual_recipe(project)
    owner_state = tmp_path / "owner-state"
    config = _runtime_config(tmp_path, tmp_path / "provider-invoked")
    recipes = project / ".lockstep" / "recipes"
    index = RuntimeRequirementIndex.for_authorized_closures(
        (preflight_recipe(recipes, "target"),),
        project_identity=str(project.resolve()),
    )
    granted = tuple(item.grant_selection_key for item in index.requirements)
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(owner_state))
    assert _provision(
        tmp_path=tmp_path,
        project=project,
        config=config,
        replacement=granted,
        suffix="recovery-grant",
    ) == 0

    prior_service = Engine.command(owner_state, recipes)
    prior = prior_service.start("prior", {}, str(project))
    prior_service.close()
    assert prior["status"] == "awaiting"

    recovery_entered = threading.Event()
    release_recovery = threading.Event()
    start_finished = threading.Event()
    provision_finished = threading.Event()
    start_outcome: list[object] = []
    provision_outcome: list[object] = []
    service = Engine.command(owner_state, recipes)
    original_recovery = service._recover_engine_effects

    def blocked_recovery() -> None:
        recovery_entered.set()
        if not release_recovery.wait(10.0):
            raise AssertionError("timed out waiting to release cold recovery")
        original_recovery()

    monkeypatch.setattr(service, "_recover_engine_effects", blocked_recovery)

    def start() -> None:
        try:
            start_outcome.append(service.start("target", {}, str(project)))
        except BaseException as exc:
            start_outcome.append(exc)
        finally:
            start_finished.set()

    def revoke() -> None:
        try:
            provision_outcome.append(
                _provision(
                    tmp_path=tmp_path,
                    project=project,
                    config=config,
                    replacement=(),
                    suffix="recovery-revoke",
                )
            )
        except BaseException as exc:
            provision_outcome.append(exc)
        finally:
            provision_finished.set()

    start_thread = threading.Thread(target=start, name="lockstep-a1-recovery")
    provision_thread = threading.Thread(
        target=revoke, name="lockstep-a1-recovery-provision"
    )
    start_thread.start()
    try:
        assert recovery_entered.wait(10.0), "cold activation never reached recovery"
        bindings = service.catalog.list(str(project.resolve()))
        parked_before_recovery = len(bindings) == 2
        provision_thread.start()
        provision_completed_during_recovery = provision_finished.wait(2.0)
    finally:
        release_recovery.set()
        start_finished.wait(10.0)
        provision_finished.wait(10.0)
        start_thread.join(timeout=1.0)
        provision_thread.join(timeout=1.0)
        service.close()

    observed = {
        "new_park_precedes_recovery": parked_before_recovery,
        "provision_completed_during_recovery": (
            provision_completed_during_recovery
        ),
        "supported_revoke_succeeded": provision_outcome == [0],
        "start_parked": (
            len(start_outcome) == 1
            and isinstance(start_outcome[0], dict)
            and start_outcome[0].get("status") == "starting"
        ),
        "threads_stopped": (
            not start_thread.is_alive() and not provision_thread.is_alive()
        ),
    }
    assert observed == {
        "new_park_precedes_recovery": True,
        "provision_completed_during_recovery": True,
        "supported_revoke_succeeded": True,
        "start_parked": True,
        "threads_stopped": True,
    }
