"""Pure preparation and transition policy for the durable effect ledger."""

from __future__ import annotations

import json
from datetime import datetime

from lockstep.runtime.effects._ledger_records import (
    EffectRecord,
    _binding_digest,
    _dump,
    _nonempty,
)
from lockstep.runtime.effects.models import (
    AcceptDescriptor,
    AcceptanceResult,
    EffectDescriptor,
    EffectResult,
    PublishDescriptor,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.native_models import NativeCoordinate

PRELAUNCH_ERROR_CODES = frozenset({"prelaunch_failed", "deadline_timeout"})


class EffectConflict(RuntimeError):
    """An immutable effect fact conflicts with an existing fact."""


class StaleEffectRevision(RuntimeError):
    """The caller lost the optimistic concurrency race."""


class IllegalEffectTransition(RuntimeError):
    """The requested phase edge is not part of the monotonic lifecycle."""


class StaleEffectLease(RuntimeError):
    """The supplied effect lease is not the current live fence."""


def _validate_prepare_coordinate(coordinate: NativeCoordinate) -> None:
    for name in ("thread_id", "checkpoint_id", "task_id", "interrupt_id"):
        _nonempty(getattr(coordinate, name), name)
    if not isinstance(coordinate.checkpoint_ns, str):
        raise TypeError("checkpoint_ns must be a string")


def _validate_effect_preparation(
    descriptor: EffectDescriptor,
    *,
    deadline: datetime | None,
    binding: str | None,
    request: str | None,
    now: datetime,
) -> None:
    if descriptor.kind == "manual" and deadline is not None:
        raise ValueError("unmanaged manual effect may not bind a deadline")
    if descriptor.kind != "manual" and binding is None:
        raise ValueError("managed effect requires a runner binding")
    if (
        descriptor.deadline_seconds is not None or descriptor.scope_state_keys
    ) and deadline is None:
        raise ValueError("bounded effect requires its resolved deadline")
    if (
        descriptor.runner is not None
        and request is None
        and (deadline is None or deadline > now)
    ):
        raise ValueError(
            "runnable effect requires exact request and grant commitments"
        )


def _validate_prepare_descriptor(
    descriptor: EffectDescriptor | ScopeDescriptor | AcceptDescriptor | PublishDescriptor,
    *,
    deadline: datetime | None,
    binding: str | None,
    request: str | None,
    grant: str | None,
    now: datetime,
) -> None:
    if isinstance(descriptor, EffectDescriptor):
        _validate_effect_preparation(
            descriptor,
            deadline=deadline,
            binding=binding,
            request=request,
            now=now,
        )
        return
    if isinstance(descriptor, ScopeDescriptor):
        if descriptor.scope_kind == "call" and binding is None:
            raise ValueError("call scope requires a runner binding")
        return
    if isinstance(descriptor, AcceptDescriptor):
        if binding is not None or request is not None or grant is not None:
            raise ValueError("acceptance has no external launch commitment")
        return
    if binding is None or request is None or grant is None:
        raise ValueError("publication requires exact authority commitments")


def _validate_result_kind(
    current: EffectRecord,
    effect_id: str,
    result: EffectResult | ScopeResult | AcceptanceResult | None,
) -> None:
    if result is None:
        return
    if result.effect_id != effect_id:
        raise EffectConflict("result effect_id does not match ledger identity")
    if current.effect_kind == "scope" and not isinstance(result, ScopeResult):
        raise EffectConflict("effect result kind does not match scope")
    if current.effect_kind == "accept" and not isinstance(
        result, AcceptanceResult
    ):
        raise EffectConflict("acceptance result kind does not match descriptor")
    if current.effect_kind not in {"scope", "accept"} and not isinstance(
        result, EffectResult
    ):
        raise EffectConflict("effect result kind does not match descriptor")


def _validate_scope_seal(
    current: EffectRecord,
    result: EffectResult | ScopeResult | AcceptanceResult | None,
    scope_descriptor: ScopeDescriptor | None,
) -> None:
    if not isinstance(result, ScopeResult):
        return
    if scope_descriptor is None:
        raise EffectConflict("scope seal requires its validated descriptor")
    if scope_descriptor.digest != current.descriptor_digest:
        raise EffectConflict("scope descriptor does not match prepared digest")
    if result.scope_digest != current.descriptor_digest:
        raise EffectConflict("scope digest does not match descriptor")
    if result.scope_kind != scope_descriptor.scope_kind:
        raise EffectConflict("scope result kind does not match descriptor")
    if (
        result.outcome == "PASS"
        and result.runner_selector != scope_descriptor.runner_selector
    ):
        raise EffectConflict("scope runner selector does not match descriptor")
    if (
        result.outcome == "PASS"
        and result.runner_binding_digest != current.runner_binding_digest
    ):
        raise EffectConflict(
            "scope runner binding does not match prepared facts"
        )


def _validate_prelaunch_seal(
    current: EffectRecord,
    target: str,
    result: EffectResult | ScopeResult | AcceptanceResult | None,
) -> None:
    if (
        target == "sealed"
        and current.phase == "prepared"
        and isinstance(result, EffectResult)
        and current.effect_kind != "manual"
        and (
            result.outcome != "ERROR"
            or result.fixed_error_code not in PRELAUNCH_ERROR_CODES
        )
    ):
        raise IllegalEffectTransition(
            "managed pre-launch seal requires a fixed pre-launch ERROR"
        )


def _terminal_transition_replay(
    current: EffectRecord,
    target: str,
    result: EffectResult | ScopeResult | AcceptanceResult | None,
) -> EffectRecord | None:
    if current.phase in {"sealed", "indeterminate", "delivered"} and result is not None:
        if current.result == result:
            return current
        raise EffectConflict("effect is already sealed with a different result")
    if current.phase == "delivered" and target == "delivered":
        return current
    return None


def _validate_transition_facts(
    current: EffectRecord,
    *,
    target: str,
    runner_binding_digest: str | None,
    workspace_ref: str | None,
    launch_commitment_digest: str | None,
) -> tuple[str | None, str | None]:
    if target == "launching" and (
        current.request_digest is None
        or current.grant_digest is None
        or launch_commitment_digest is None
    ):
        raise EffectConflict(
            "runner launch requires request, grant, and launch commitments"
        )
    if (
        target == "sealed"
        and current.phase in {"launching", "running"}
        and runner_binding_digest is None
    ):
        raise EffectConflict("active effect seal requires its runner binding")
    if runner_binding_digest is not None:
        binding = _binding_digest(runner_binding_digest)
        if binding != current.runner_binding_digest:
            raise EffectConflict(
                "effect runner binding does not match prepared facts"
            )
    normalized_workspace = workspace_ref
    if workspace_ref is not None:
        normalized_workspace = _nonempty(workspace_ref, "workspace_ref")
        if (
            target == "launching"
            and current.workspace_ref is not None
            and current.workspace_ref != normalized_workspace
        ):
            raise EffectConflict(
                "effect already has a different prepared workspace"
            )
    return normalized_workspace, _binding_digest(launch_commitment_digest)


def _transition_values(
    current: EffectRecord,
    *,
    target: str,
    lease: Lease | None,
    workspace_ref: str | None,
    launch_digest: str | None,
    result: EffectResult | ScopeResult | AcceptanceResult | None,
    now: datetime,
) -> tuple[dict[str, object], str | None, int, datetime]:
    revision = current.revision + 1
    changes: dict[str, object] = {
        "phase": target,
        "revision": revision,
        "updated_at": _dump(now),
    }
    if lease is not None:
        changes["lease_epoch"] = lease.epoch
    if target == "launching" and workspace_ref is not None:
        changes["workspace_ref"] = workspace_ref
    if target == "launching":
        changes["launch_commitment_digest"] = launch_digest
    result_json = None
    if result is not None:
        changes["result_ref"] = getattr(result, "result_ref", None)
        changes["fixed_error_code"] = getattr(
            result, "fixed_error_code", None
        )
        result_json = json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    return changes, result_json, revision, now
