"""Pure helpers shared by command and passive runtime observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.native_models import NativeSnapshot
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json
from lockstep.runtime.status import project_status


def status_revision(value: Mapping[str, Any]) -> str:
    try:
        admitted = bounded_json(value, label="scenario wait observation")
        encoded = json.dumps(
            admitted,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (PayloadLimitExceeded, TypeError, ValueError) as exc:
        raise LockstepError("scenario wait observation is invalid") from exc
    return "revision:" + hashlib.sha256(encoded).hexdigest()


def project_history(
    binding: RunBinding, snapshots: Iterable[NativeSnapshot]
) -> list[dict[str, Any]]:
    return [
        {
            "checkpoint_id": item.checkpoint_id,
            "checkpoint_ns": item.checkpoint_ns,
            "created_at": item.created_at,
            "status": project_status(binding, item, (), ()).status,
        }
        for item in snapshots
    ]


def project_events(
    native: Sequence[NativeSnapshot],
    effects: Sequence[object],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if len(native) + len(effects) > limit:
        raise LockstepError("event observations exceed public bound")
    observed: list[dict[str, Any]] = [
        {
            "source": "native",
            "checkpoint_id": item.checkpoint_id,
            "checkpoint_ns": item.checkpoint_ns,
            "created_at": item.created_at,
            "next": list(item.next),
            "pending_count": len(item.pending),
            "error_count": len(item.task_errors),
        }
        for item in native
    ]
    observed.extend(
        {
            "source": "effect",
            "effect_id": item.effect_id,
            "effect_kind": item.effect_kind,
            "phase": str(item.phase),
            "updated_at": (
                item.updated_at.isoformat()
                if hasattr(item.updated_at, "isoformat")
                else item.updated_at
            ),
        }
        for item in effects
    )
    return observed


def project_trace(history: Iterable[Mapping[str, Any]]) -> str:
    return "\n".join(str(dict(item)) for item in history)
