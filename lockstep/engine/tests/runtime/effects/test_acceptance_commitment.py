from __future__ import annotations

import pytest

from lockstep.runtime.effects.descriptors import (
    parse_acceptance_result,
    parse_effect_descriptor,
)
from lockstep.runtime.effects.models import AcceptDescriptor


def _descriptor() -> AcceptDescriptor:
    value = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "accept",
            "logical_id": "accept-review",
            "artifact_handle": "review.report",
            "producer_result_state_key": "review_result",
            "declared_name": "report",
            "destination": "docs/review.md",
            "transformation": "identity",
            "audience": "local-project",
            "verdict": "PASS",
            "result_schema": "lockstep.acceptance-result/v1",
        }
    )
    assert isinstance(value, AcceptDescriptor)
    return value


def _result() -> dict:
    return {
        "schema": "lockstep.acceptance-result/v1",
        "effect_id": "effect-1",
        "outcome": "PASS",
        "artifact_ref": "artifact:" + "a" * 64,
        "artifact_digest": "b" * 64,
        "destination": "docs/review.md",
        "transformation": "identity",
        "audience": "local-project",
        "consent_ref": "consent:owner-issued-1",
        "approval_generation": 7,
    }


def test_acceptance_result_binds_the_exact_publication_commitment() -> None:
    descriptor = _descriptor()
    parsed = parse_acceptance_result(_result(), descriptor=descriptor)

    assert parsed.to_dict() == _result()


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("destination", "docs/other.md"),
        ("transformation", "rewrite"),
        ("audience", "external"),
    ],
)
def test_acceptance_commitment_mismatch_is_rejected(
    field: str, changed: object
) -> None:
    descriptor = _descriptor()
    value = {**_result(), field: changed}

    with pytest.raises(ValueError, match="commitment|descriptor"):
        parse_acceptance_result(value, descriptor=descriptor)
