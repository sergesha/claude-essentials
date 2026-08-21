"""Process-free handoff boundary for protected manual effects."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from lockstep.runtime.blobs import BlobRef, BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import (
    derive_effect_id,
    parse_effect_descriptor,
    parse_effect_result,
)
from lockstep.runtime.effects.models import EffectDescriptor, EffectResult
from lockstep.runtime.manifests import (
    ProjectWritePath,
    capture_project,
    compare_effect,
    snapshot_from_data,
    snapshot_to_data,
)
from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt
from lockstep.runtime.owner_state import (
    ensure_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    verify_owner_file,
)
from lockstep.runtime.payload_limits import bounded_json


class ManualProviderError(RuntimeError):
    """A manual handoff or result does not match its immutable boundary."""


def _canonical(value: object, *, label: str) -> bytes:
    admitted = bounded_json(value, label=label)
    return json.dumps(
        admitted,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class ManualHandoff:
    effect_id: str
    public_run_id: str
    coordinate: NativeCoordinate
    descriptor_digest: str
    project_identity: str
    writes: tuple[str, ...]
    baseline: BlobRef
    write_contract_digest: str
    digest: str


@dataclass(frozen=True)
class ManualSubmission:
    kind: Literal["done", "escalate", "abort"]
    payload: bytes

    @classmethod
    def build(
        cls,
        outcome: Literal["PASS", "FAIL", "ABORTED"],
        *,
        evidence: object | None = None,
        reason: object | None = None,
    ) -> ManualSubmission:
        if outcome == "PASS":
            if reason is not None:
                raise ValueError("manual PASS cannot carry an escalation reason")
            checked = bounded_json(
                {} if evidence is None else evidence, label="manual evidence"
            )
            if not isinstance(checked, dict):
                raise ValueError("manual evidence must be a JSON object")
            if any(not isinstance(key, str) or key.startswith("_") for key in checked):
                raise ValueError("reserved manual evidence keys are forbidden")
            return cls("done", _canonical(checked, label="manual evidence"))
        if evidence is not None:
            raise ValueError("manual control submission cannot carry evidence")
        if outcome == "FAIL":
            checked_reason = bounded_json(reason, label="manual escalation reason")
            if not isinstance(checked_reason, str) or not checked_reason:
                raise ValueError("manual escalation reason must be a non-empty string")
            return cls(
                "escalate",
                _canonical(checked_reason, label="manual escalation reason"),
            )
        if outcome == "ABORTED":
            if reason is not None:
                raise ValueError("manual abort cannot carry a reason")
            return cls("abort", b"null")
        raise ValueError("unknown manual submission outcome")


class ManualProvider:
    """Persist a project baseline before handing authority to a human worker.

    This adapter has deliberately no process lifecycle API.  The effect ledger
    remains the only owner of attempt phases and the graph remains the only owner
    of workflow progress.
    """

    def __init__(self, owner_state_dir: str | Path, blobs: BlobStore) -> None:
        self._owner = initialize_owner_state(owner_state_dir)
        self._records = ensure_owner_directory(self._owner, "manual-handoffs")
        self._blobs = blobs

    @staticmethod
    def _raw_descriptor(interrupt: NativeInterrupt) -> object:
        if not isinstance(interrupt.value, dict):
            raise ManualProviderError("manual interrupt payload must be an object")
        return interrupt.value.get("lockstep_effect")

    @staticmethod
    def _record_name(effect_id: str) -> str:
        return hashlib.sha256(effect_id.encode("utf-8")).hexdigest() + ".json"

    def _path(self, effect_id: str) -> Path:
        return self._records / self._record_name(effect_id)

    @staticmethod
    def _handoff_data(handoff: ManualHandoff) -> dict[str, Any]:
        return {
            "schema": "lockstep.manual-handoff/v1",
            "effect_id": handoff.effect_id,
            "public_run_id": handoff.public_run_id,
            "coordinate": {
                "thread_id": handoff.coordinate.thread_id,
                "checkpoint_ns": handoff.coordinate.checkpoint_ns,
                "checkpoint_id": handoff.coordinate.checkpoint_id,
                "task_id": handoff.coordinate.task_id,
                "interrupt_id": handoff.coordinate.interrupt_id,
            },
            "descriptor_digest": handoff.descriptor_digest,
            "project_identity": handoff.project_identity,
            "writes": list(handoff.writes),
            "baseline": {
                "sha256": handoff.baseline.sha256,
                "size": handoff.baseline.size,
            },
            "write_contract_digest": handoff.write_contract_digest,
            "digest": handoff.digest,
        }

    def _write_once(self, handoff: ManualHandoff) -> None:
        encoded = _canonical(self._handoff_data(handoff), label="manual handoff record")
        path = self._path(handoff.effect_id)
        if path.exists() or path.is_symlink():
            verify_owner_file(path)
            if path.read_bytes() != encoded:
                raise ManualProviderError(
                    "manual effect is already bound to another handoff"
                )
            return
        descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(temporary, writable=False)
            try:
                os.link(temporary, path)
            except FileExistsError:
                self._write_once(handoff)
                return
        finally:
            temporary.unlink(missing_ok=True)

    def lookup(self, effect_id: str) -> ManualHandoff:
        path = self._path(effect_id)
        try:
            verify_owner_file(path)
            data = json.loads(path.read_bytes())
            required = {
                "schema",
                "effect_id",
                "public_run_id",
                "coordinate",
                "descriptor_digest",
                "project_identity",
                "writes",
                "baseline",
                "write_contract_digest",
                "digest",
            }
            if (
                not isinstance(data, dict)
                or set(data) != required
                or data["schema"] != "lockstep.manual-handoff/v1"
                or data["effect_id"] != effect_id
            ):
                raise ManualProviderError("invalid manual handoff record")
            raw_coordinate = data["coordinate"]
            raw_baseline = data["baseline"]
            if not isinstance(raw_coordinate, dict) or set(raw_coordinate) != {
                "thread_id",
                "checkpoint_ns",
                "checkpoint_id",
                "task_id",
                "interrupt_id",
            }:
                raise ManualProviderError("invalid manual handoff coordinate")
            if not isinstance(raw_baseline, dict) or set(raw_baseline) != {
                "sha256",
                "size",
            }:
                raise ManualProviderError("invalid manual handoff baseline")
            writes = data["writes"]
            if not isinstance(writes, list) or not all(
                isinstance(item, str) for item in writes
            ):
                raise ManualProviderError("invalid manual handoff writes")
            handoff = ManualHandoff(
                effect_id=effect_id,
                public_run_id=data["public_run_id"],
                coordinate=NativeCoordinate(**raw_coordinate),
                descriptor_digest=data["descriptor_digest"],
                project_identity=data["project_identity"],
                writes=tuple(writes),
                baseline=BlobRef(
                    sha256=raw_baseline["sha256"], size=raw_baseline["size"]
                ),
                write_contract_digest=data["write_contract_digest"],
                digest=data["digest"],
            )
            self._blobs.read(handoff.baseline)
            contract = {
                "schema": "lockstep.manual-write-contract/v1",
                "effect_id": handoff.effect_id,
                "public_run_id": handoff.public_run_id,
                "coordinate": raw_coordinate,
                "descriptor_digest": handoff.descriptor_digest,
                "project_identity": handoff.project_identity,
                "writes": list(handoff.writes),
                "baseline_sha256": handoff.baseline.sha256,
                "baseline_size": handoff.baseline.size,
            }
            contract_digest = hashlib.sha256(
                _canonical(contract, label="manual write contract")
            ).hexdigest()
            digest = hashlib.sha256(
                _canonical(
                    {**contract, "write_contract_digest": contract_digest},
                    label="manual handoff",
                )
            ).hexdigest()
            if (
                handoff.write_contract_digest != contract_digest
                or handoff.digest != digest
            ):
                raise ManualProviderError("manual handoff digest mismatch")
            return handoff
        except FileNotFoundError as exc:
            raise KeyError(effect_id) from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, KeyError) and exc.args == (effect_id,):
                raise
            raise ManualProviderError("invalid manual handoff record") from exc

    def prepare_handoff(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor,
    ) -> ManualHandoff:
        if descriptor.kind != "manual" or descriptor.runner is not None:
            raise ManualProviderError(
                "manual provider accepts only unmanaged manual effects"
            )
        if descriptor.deadline_seconds is not None or descriptor.scope_state_keys:
            raise ManualProviderError("unmanaged manual handoff cannot be bounded")
        if descriptor.artifacts:
            raise ManualProviderError(
                "manual artifacts require the ArtifactRegistry boundary"
            )
        parsed = parse_effect_descriptor(self._raw_descriptor(interrupt))
        if not isinstance(parsed, EffectDescriptor) or parsed != descriptor:
            raise ManualProviderError("manual handoff descriptor changed")
        if interrupt.coordinate.thread_id != binding.thread_id:
            raise ManualProviderError("manual handoff belongs to another thread")
        project = Path(binding.project_identity).resolve()
        if not project.is_dir():
            raise ManualProviderError("manual project is unavailable")
        effect_id = derive_effect_id(interrupt.coordinate, descriptor.digest)
        try:
            existing = self.lookup(effect_id)
        except KeyError:
            existing = None
        if existing is not None:
            expected = (
                binding.public_run_id,
                interrupt.coordinate,
                descriptor.digest,
                str(project),
                descriptor.writes,
            )
            observed = (
                existing.public_run_id,
                existing.coordinate,
                existing.descriptor_digest,
                existing.project_identity,
                existing.writes,
            )
            if observed != expected:
                raise ManualProviderError(
                    "manual effect is already bound to another handoff"
                )
            return existing
        baseline_data = _canonical(
            snapshot_to_data(capture_project(project)),
            label="manual project baseline",
        )
        baseline = self._blobs.put(baseline_data)
        contract = {
            "schema": "lockstep.manual-write-contract/v1",
            "effect_id": effect_id,
            "public_run_id": binding.public_run_id,
            "coordinate": {
                "thread_id": interrupt.coordinate.thread_id,
                "checkpoint_ns": interrupt.coordinate.checkpoint_ns,
                "checkpoint_id": interrupt.coordinate.checkpoint_id,
                "task_id": interrupt.coordinate.task_id,
                "interrupt_id": interrupt.coordinate.interrupt_id,
            },
            "descriptor_digest": descriptor.digest,
            "project_identity": str(project),
            "writes": list(descriptor.writes),
            "baseline_sha256": baseline.sha256,
            "baseline_size": baseline.size,
        }
        contract_bytes = _canonical(contract, label="manual write contract")
        write_contract_digest = hashlib.sha256(contract_bytes).hexdigest()
        handoff_digest = hashlib.sha256(
            _canonical(
                {
                    **contract,
                    "write_contract_digest": write_contract_digest,
                },
                label="manual handoff",
            )
        ).hexdigest()
        handoff = ManualHandoff(
            effect_id=effect_id,
            public_run_id=binding.public_run_id,
            coordinate=interrupt.coordinate,
            descriptor_digest=descriptor.digest,
            project_identity=str(project),
            writes=descriptor.writes,
            baseline=baseline,
            write_contract_digest=write_contract_digest,
            digest=handoff_digest,
        )
        self._write_once(handoff)
        return handoff

    def submit(
        self, handoff: ManualHandoff, submission: ManualSubmission
    ) -> EffectResult:
        self._write_once(handoff)
        project = Path(handoff.project_identity)
        before = snapshot_from_data(json.loads(self._blobs.read(handoff.baseline)))
        after = capture_project(project)
        allowed = tuple(
            ProjectWritePath.parse(path, project) for path in handoff.writes
        )
        claimed = {
            "done": "pass",
            "escalate": "fail",
            "abort": "error",
        }[submission.kind]
        comparison = compare_effect(before, after, allowed, claimed)
        payload = self._blobs.put(submission.payload)
        if comparison.integrity_error:
            outcome = "ERROR"
            fixed_error_code = "manifest_invalid"
        elif submission.kind == "done":
            outcome = "PASS"
            fixed_error_code = None
        elif submission.kind == "escalate":
            outcome = "FAIL"
            fixed_error_code = None
        else:
            outcome = "ERROR"
            fixed_error_code = "cancelled"
        return parse_effect_result(
            {
                "schema": "lockstep.effect-result/v1",
                "effect_id": handoff.effect_id,
                "outcome": outcome,
                "result_ref": None,
                "artifact_refs": [],
                "snapshot_ref": None,
                "diff_ref": None,
                "fixed_error_code": fixed_error_code,
                "evidence_refs": [f"blob:{payload.sha256}"],
            }
        )

    complete = submit
