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
        "receipt_digest": "c" * 64,
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


@pytest.mark.parametrize(
    "receipt_digest",
    [None, "", "C" * 64, "c" * 63, "not-a-digest"],
)
def test_acceptance_result_requires_an_owner_receipt_digest(
    receipt_digest: object,
) -> None:
    value = _result()
    if receipt_digest is None:
        del value["receipt_digest"]
    else:
        value["receipt_digest"] = receipt_digest

    with pytest.raises((TypeError, ValueError), match="receipt|closed|field"):
        parse_acceptance_result(value, descriptor=_descriptor())


@pytest.mark.parametrize("field", ["token", "session_id", "runner"])
def test_acceptance_result_rejects_caller_authority_fields(field: str) -> None:
    with pytest.raises(ValueError, match="unknown|field|closed"):
        parse_acceptance_result(
            {**_result(), field: "caller-asserted"}, descriptor=_descriptor()
        )


@pytest.mark.parametrize(
    "content",
    (
        "PASS",
        "# Verdict\nPASS\n",
        {"markdown": {"sections": ["Findings", "Verdict"]}, "text": "PASS"},
    ),
)
def test_report_text_and_heading_metadata_never_create_acceptance_authority(
    content: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_acceptance_result(content, descriptor=_descriptor())


@pytest.mark.parametrize(
    "field",
    ("markdown", "headings", "verdict_text", "bearer", "token"),
)
def test_accept_descriptor_rejects_prompt_metadata_and_caller_bearers(
    field: str,
) -> None:
    raw = {
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
        field: "PASS",
    }

    with pytest.raises(ValueError, match="unknown|field|closed"):
        parse_effect_descriptor(raw)
