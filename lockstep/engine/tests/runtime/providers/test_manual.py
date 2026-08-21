from __future__ import annotations

from pathlib import Path

from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt


def _manual_descriptor() -> dict[str, object]:
    return {
        "schema": "lockstep.effect/v1",
        "kind": "manual",
        "logical_id": "edit",
        "runner": None,
        "inputs": {},
        "writes": ["src/"],
        "artifacts": [],
        "deadline_seconds": None,
        "scope_state_keys": [],
        "result_schema": "lockstep.effect-result/v1",
    }


def test_manual_handoff_captures_baseline_before_allowed_edit(tmp_path: Path) -> None:
    from lockstep.runtime.providers.manual import ManualProvider, ManualSubmission

    owner = tmp_path / "owner"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    target = project / "src/app.py"
    target.write_text("VALUE = 1\n")
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(project)
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    descriptor = parse_effect_descriptor(_manual_descriptor())
    interrupt = NativeInterrupt(
        coordinate, {"lockstep_effect": _manual_descriptor()}
    )
    provider = ManualProvider(owner, BlobStore(owner))

    handoff = provider.prepare_handoff(binding, interrupt, descriptor)
    target.write_text("VALUE = 2\n")
    result = provider.submit(
        handoff,
        ManualSubmission.build("PASS", evidence={"reviewed": True}),
    )

    assert result.effect_id == handoff.effect_id
    assert result.outcome == "PASS"
    assert result.fixed_error_code is None
    assert len(result.evidence_refs) == 1


def test_manual_manifest_is_checked_on_fail_not_only_pass(tmp_path: Path) -> None:
    from lockstep.runtime.providers.manual import ManualProvider, ManualSubmission

    owner = tmp_path / "owner"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "outside.txt").write_text("before")
    binding = RunBinding(
        "run-1", "thread-1", "a" * 64, "bundle:" + "b" * 64, str(project)
    )
    coordinate = NativeCoordinate("thread-1", "cp-1", "", "task-1", "int-1")
    descriptor = parse_effect_descriptor(_manual_descriptor())
    interrupt = NativeInterrupt(
        coordinate, {"lockstep_effect": _manual_descriptor()}
    )
    provider = ManualProvider(owner, BlobStore(owner))
    handoff = provider.prepare_handoff(binding, interrupt, descriptor)
    (project / "outside.txt").write_text("forbidden")

    result = provider.submit(
        handoff,
        ManualSubmission.build("FAIL", reason="blocked"),
    )

    assert result.outcome == "ERROR"
    assert result.fixed_error_code == "manifest_invalid"

