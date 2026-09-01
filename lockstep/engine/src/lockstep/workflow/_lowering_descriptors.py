"""Public lowering helpers for artifact authority descriptors."""

from __future__ import annotations

from typing import Any, Literal

from lockstep.runtime.effects.descriptors import parse_effect_descriptor


def lower_accept_descriptor(
    logical_id: str,
    artifact_handle: str,
    producer_result_state_key: str,
    declared_name: str,
    destination: str,
    transformation: Literal["identity"] = "identity",
    audience: Literal["local-project"] = "local-project",
) -> dict[str, Any]:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "accept",
        "logical_id": logical_id,
        "artifact_handle": artifact_handle,
        "producer_result_state_key": producer_result_state_key,
        "declared_name": declared_name,
        "destination": destination,
        "transformation": transformation,
        "audience": audience,
        "verdict": "PASS",
        "result_schema": "lockstep.acceptance-result/v1",
    }
    parse_effect_descriptor(descriptor)
    return descriptor


def lower_publish_descriptor(
    logical_id: str,
    *,
    artifact_handle: str,
    producer_result_state_key: str,
    declared_name: str,
    acceptance_result_state_key: str,
    destination: str,
) -> dict[str, Any]:
    descriptor = {
        "schema": "lockstep.effect/v1",
        "kind": "publish",
        "logical_id": logical_id,
        "items": [
            {
                "qualified_handle": artifact_handle,
                "producer_result_state_key": producer_result_state_key,
                "declared_name": declared_name,
                "acceptance_result_state_key": acceptance_result_state_key,
                "destination": destination,
                "transformation": "identity",
                "audience": "local-project",
            }
        ],
        "result_schema": "lockstep.effect-result/v1",
    }
    parse_effect_descriptor(descriptor)
    return descriptor
