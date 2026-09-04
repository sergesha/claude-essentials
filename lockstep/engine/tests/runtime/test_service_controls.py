from __future__ import annotations

import hashlib
import json
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from lockstep.runtime import sessions
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt
from lockstep.runtime.owner_state import initialize_owner_state
from lockstep.runtime.providers.manual import ManualSubmission
from lockstep.runtime.worker_submission_service import WorkerSubmissionService


def _protected_manual_service(
    tmp_path: Path,
    *,
    checks: list[dict[str, object]],
    state_values: dict[str, object] | None = None,
):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    state = initialize_owner_state(tmp_path / "owner-state")
    sessions.touch(state, "run-1", "session-1", 30)
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    raw_descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "edit",
        "runner": None,
        "inputs": {},
        "writes": [],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }
    descriptor = parse_effect_descriptor(raw_descriptor)
    interrupt = NativeInterrupt(
        coordinate,
        {
            "step": "edit",
            "evidence_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "format": "project-path"}
                },
            },
            "checks": checks,
            "lockstep_effect": raw_descriptor,
        },
        state_values=state_values,
    )
    binding = SimpleNamespace(project_identity=str(project))

    class Coordinator:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def submit_manual(self, *args: object) -> None:
            self.calls.append(args)

    class Leases:
        def acquire(self, *_args: object) -> object:
            return object()

        def release(self, _lease: object) -> None:
            return None

    coordinator = Coordinator()
    service = WorkerSubmissionService(
        state_dir=state,
        runtime=SimpleNamespace(
            resume=lambda *_args, **_kwargs: pytest.fail("native graph resumed")
        ),
        manual_effect_resources=lambda: (Leases(), coordinator),
        admission_lock=threading.RLock(),
        validate_existing=lambda *_args: None,
        bind_existing=lambda *_args: nullcontext(binding),
        select_interrupt=lambda *_args: (binding, interrupt),
        protected_descriptor=lambda _interrupt: descriptor,
        drive_engine_owned=lambda *_args, **_kwargs: pytest.fail(
            "manual rejection drove the graph"
        ),
    )
    return service, project, coordinator


@pytest.mark.parametrize("category", ("project-read", "baseline-read", "malformed"))
def test_protected_manual_completion_enforces_nonexecuting_declared_checks(
    tmp_path: Path,
    category: str,
) -> None:
    if category == "project-read":
        checks = [{"type": "file_matches", "path_from": "path", "regex": "PASS"}]
        state_values = None
        evidence = {"path": "missing.md"}
        expected = "file_matches"
    elif category == "baseline-read":
        project = tmp_path / "project"
        source = project / "src" / "change.py"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"changed\n")
        manifest = tmp_path / "previous.json"
        manifest.write_text(
            json.dumps({"src/change.py": hashlib.sha256(b"before\n").hexdigest()}),
            encoding="utf-8",
        )
        checks = [{"type": "unchanged", "glob": "src/**", "since": "previous"}]
        state_values = {
            "_baseline_prev": str(manifest),
            "_baseline_globs": ["src/**"],
        }
        evidence = {}
        expected = "unchanged"
    else:
        checks = [{"type": ["file_exists"]}]
        state_values = None
        evidence = {}
        expected = "unknown check type"

    service, _project, coordinator = _protected_manual_service(
        tmp_path,
        checks=checks,
        state_values=state_values,
    )

    with pytest.raises(LockstepError, match=expected):
        service.resume(
            "run-1",
            "edit",
            {"evidence": evidence},
            manual_submission=ManualSubmission.build("PASS", evidence=evidence),
            session_id="session-1",
            project=str(_project),
        )

    assert coordinator.calls == []


def test_protected_manual_completion_cannot_execute_a_declared_command(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "command-ran"
    service, project, coordinator = _protected_manual_service(
        tmp_path,
        checks=[
            {
                "type": "junit_gate",
                "command": f"python -c 'from pathlib import Path; Path({str(marker)!r}).touch()'",
                "min_tests": 1,
            }
        ],
    )

    with pytest.raises(LockstepError, match="pinned execution"):
        service.resume(
            "run-1",
            "edit",
            {"evidence": {}},
            manual_submission=ManualSubmission.build("PASS", evidence={}),
            session_id="session-1",
            project=str(project),
        )

    assert marker.exists() is False
    assert coordinator.calls == []


def test_protected_manual_validation_uses_the_canonical_submission_payload(
    tmp_path: Path,
) -> None:
    service, project, coordinator = _protected_manual_service(tmp_path, checks=[])

    with pytest.raises(LockstepError, match="does not match submission"):
        service.resume(
            "run-1",
            "edit",
            {"evidence": {"path": "caller-copy.md"}},
            manual_submission=ManualSubmission.build(
                "PASS", evidence={"path": "submitted.md"}
            ),
            session_id="session-1",
            project=str(project),
        )

    assert coordinator.calls == []
