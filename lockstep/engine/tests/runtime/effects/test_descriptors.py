from __future__ import annotations

from datetime import datetime, timezone

import pytest


def managed_descriptor(**changes):
    value = {
        "schema": "lockstep.effect/v1",
        "kind": "managed",
        "logical_id": "implement",
        "runner": {
            "selector": "codex",
            "required_capabilities": ["workspace", "bounded_result"],
        },
        "inputs": {
            "brief": {"state_key": "implement_brief"},
            "snapshot": {"state_key": "project_snapshot_ref"},
        },
        "writes": ["src/", "tests/test_feature.py"],
        "artifacts": [
            {
                "name": "review",
                "source_path": "src/review.md",
                "media_type": "text/markdown",
                "required": True,
            }
        ],
        "deadline_seconds": 1800,
        "scope_state_keys": ["call_scope"],
        "result_schema": "lockstep.effect-result/v1",
    }
    value.update(changes)
    return value


def test_descriptor_is_closed_bounded_and_canonical() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    parsed = parse_effect_descriptor(managed_descriptor())

    assert parsed.kind == "managed"
    assert parsed.writes == ("src/", "tests/test_feature.py")
    assert (
        parsed.digest
        == "a312435d29a68ce96d141178bff917a8ce3b0a62a08f9407bdd6a20f671a9f8d"
    )
    assert parsed.canonical_json.startswith(b'{"artifacts":')


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"schema": "lockstep.effect/v2"}, "schema"),
        ({"kind": "shell"}, "kind"),
        ({"command": "rm -rf project"}, "unknown"),
        ({"deadline_seconds": True}, "deadline"),
        ({"deadline_seconds": 0}, "deadline"),
        ({"writes": ["../outside"]}, "write"),
        ({"writes": ["/absolute"]}, "write"),
        ({"writes": ["src/*.py"]}, "write"),
        ({"writes": [".git/config"]}, "write"),
        ({"writes": ["src//generated"]}, "write"),
        ({"writes": ["src/\x00escape"]}, "NUL"),
        ({"inputs": {"brief": {"expression": "__import__('os')"}}}, "selector"),
        (
            {
                "runner": {
                    "selector": "codex",
                    "required_capabilities": ["workspace"],
                    "argv": ["sh", "-c", "payload"],
                }
            },
            "unknown",
        ),
    ],
)
def test_descriptor_rejects_unknown_dynamic_and_unsafe_authority(change, match) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    with pytest.raises(ValueError, match=match):
        parse_effect_descriptor(managed_descriptor(**change))


@pytest.mark.parametrize(
    "artifact",
    [
        {"name": "review", "source_path": "../review.md", "media_type": "text/markdown", "required": True},
        {"name": "review", "source_path": ".git/config", "media_type": "text/plain", "required": True},
        {"name": "review", "source_path": "docs/review.md", "media_type": "text/markdown", "required": True},
    ],
)
def test_artifact_source_is_exact_safe_and_covered_by_declared_writes(artifact) -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    with pytest.raises(ValueError, match="artifact source"):
        parse_effect_descriptor(managed_descriptor(artifacts=[artifact]))


def test_descriptor_rejects_more_than_32_artifact_declarations() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    artifacts = [
        {
            "name": f"artifact-{index}",
            "source_path": f"src/artifact-{index}.txt",
            "media_type": "text/plain",
            "required": True,
        }
        for index in range(33)
    ]

    with pytest.raises(ValueError, match="declaration limit"):
        parse_effect_descriptor(managed_descriptor(artifacts=artifacts))


def test_descriptor_rejects_callable_and_bounded_input_before_encoding() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    with pytest.raises(ValueError, match="JSON|descriptor"):
        parse_effect_descriptor(managed_descriptor(inputs={"brief": lambda: "boom"}))

    deep: object = "leaf"
    for _ in range(20):
        deep = [deep]
    with pytest.raises(ValueError, match="structural"):
        parse_effect_descriptor(managed_descriptor(inputs={"brief": deep}))


def test_descriptor_rejects_state_selectors_missing_from_declared_graph_state() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    with pytest.raises(ValueError, match="unknown state selector"):
        parse_effect_descriptor(
            managed_descriptor(),
            known_state_keys={"implement_brief", "call_scope"},
        )

    parsed = parse_effect_descriptor(
        managed_descriptor(),
        known_state_keys={
            "implement_brief",
            "project_snapshot_ref",
            "call_scope",
        },
    )
    assert parsed.logical_id == "implement"


def test_descriptor_digest_binds_authority_fields_and_declaration_order() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    original = parse_effect_descriptor(managed_descriptor())
    changed_runner = parse_effect_descriptor(
        managed_descriptor(
            runner={
                "selector": "claude",
                "required_capabilities": ["workspace", "bounded_result"],
            }
        )
    )
    changed_writes = parse_effect_descriptor(
        managed_descriptor(writes=["tests/test_feature.py", "src/"])
    )

    assert len({original.digest, changed_runner.digest, changed_writes.digest}) == 3

    with pytest.raises(ValueError, match="digest mismatch"):
        parse_effect_descriptor(managed_descriptor(), expected_digest="0" * 64)


def test_manual_effect_cannot_claim_deadline_or_bounded_scope() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    base = managed_descriptor(kind="manual", runner=None, deadline_seconds=None)
    base.pop("scope_state_keys")
    assert parse_effect_descriptor(base).kind == "manual"

    with pytest.raises(ValueError, match="manual.*deadline"):
        parse_effect_descriptor({**base, "deadline_seconds": 60})
    with pytest.raises(ValueError, match="manual.*bounded scope"):
        parse_effect_descriptor({**base, "scope_state_keys": ["scope"]})


def test_publish_descriptor_is_no_spawn_and_binds_exact_destinations() -> None:
    """Catches runner-shaped publication or destinations chosen after authority."""
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    descriptor = parse_effect_descriptor(
        {
            "schema": "lockstep.effect/v1",
            "kind": "publish",
            "logical_id": "publish-review",
            "items": [
                {
                    "qualified_handle": "review.review",
                    "producer_result_state_key": "call_review_review_result",
                    "declared_name": "review",
                    "acceptance_result_state_key": "accept_review_result",
                    "destination": ".lockstep/review.md",
                    "transformation": "identity",
                    "audience": "local-project",
                }
            ],
            "result_schema": "lockstep.effect-result/v1",
        },
        known_state_keys={"call_review_review_result", "accept_review_result"},
    )

    assert descriptor.kind == "publish"
    assert not hasattr(descriptor, "runner")
    assert descriptor.items[0].destination == ".lockstep/review.md"
    assert descriptor.items[0].producer_result_state_key == (
        "call_review_review_result"
    )


def scope_descriptor(**changes):
    value = {
        "schema": "lockstep.effect/v1",
        "kind": "scope",
        "logical_id": "review-call",
        "scope_kind": "call",
        "duration_seconds": 600,
        "runner_selector": "codex",
        "ancestor_deadline_state_keys": ["outer_scope"],
        "result_state_key": "review_scope",
        "result_schema": "lockstep.scope-result/v1",
    }
    value.update(changes)
    return value


def test_scope_descriptor_is_closed_and_parallel_has_no_runner() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor

    call = parse_effect_descriptor(scope_descriptor())
    parallel = parse_effect_descriptor(
        scope_descriptor(
            scope_kind="parallel",
            runner_selector=None,
            ancestor_deadline_state_keys=[],
        )
    )
    assert call.scope_kind == "call"
    assert parallel.scope_kind == "parallel"

    with pytest.raises(ValueError, match="runner_selector"):
        parse_effect_descriptor(scope_descriptor(scope_kind="parallel"))
    with pytest.raises(ValueError, match="unknown"):
        parse_effect_descriptor(scope_descriptor(timer_id="invented"))


def test_scope_result_variants_are_discriminated_and_closed() -> None:
    from lockstep.runtime.effects.descriptors import parse_scope_result

    passed = parse_scope_result(
        {
            "schema": "lockstep.scope-result/v1",
            "effect_id": "effect-1",
            "outcome": "PASS",
            "scope_kind": "call",
            "scope_digest": "a" * 64,
            "absolute_deadline": "2026-08-20T11:00:00+00:00",
            "runner_selector": "codex",
            "runner_binding_digest": "b" * 64,
        }
    )
    assert passed.absolute_deadline == datetime(2026, 8, 20, 11, tzinfo=timezone.utc)

    error = parse_scope_result(
        {
            "schema": "lockstep.scope-result/v1",
            "effect_id": "effect-1",
            "outcome": "ERROR",
            "scope_kind": "parallel",
            "scope_digest": "a" * 64,
            "fixed_error_code": "scope_timeout",
        }
    )
    assert error.fixed_error_code == "scope_timeout"

    with pytest.raises(ValueError, match="mixed|unknown"):
        parse_scope_result(
            {
                "schema": "lockstep.scope-result/v1",
                "effect_id": "effect-1",
                "outcome": "ERROR",
                "scope_kind": "parallel",
                "scope_digest": "a" * 64,
                "fixed_error_code": "scope_timeout",
                "absolute_deadline": None,
            }
        )


def test_scope_deadline_uses_minimum_and_expired_ancestor_fails_without_spawn() -> None:
    from lockstep.runtime.effects.descriptors import (
        build_scope_result,
        parse_scope_result,
    )

    now = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
    ancestor = parse_scope_result(
        {
            "schema": "lockstep.scope-result/v1",
            "effect_id": "outer",
            "outcome": "PASS",
            "scope_kind": "parallel",
            "scope_digest": "a" * 64,
            "absolute_deadline": "2026-08-20T10:05:00+00:00",
        }
    )
    result = build_scope_result(
        effect_id="inner",
        scope_digest="b" * 64,
        scope_kind="call",
        now=now,
        duration_seconds=600,
        ancestors=(ancestor,),
        runner_selector="codex",
        runner_binding_digest="c" * 64,
    )
    assert result.absolute_deadline == datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc)

    expired = build_scope_result(
        effect_id="late",
        scope_digest="d" * 64,
        scope_kind="parallel",
        now=datetime(2026, 8, 20, 10, 6, tzinfo=timezone.utc),
        duration_seconds=None,
        ancestors=(ancestor,),
    )
    assert expired.outcome == "ERROR"
    assert expired.fixed_error_code == "scope_timeout"


def test_member_effect_deadline_is_minimum_of_own_and_all_scope_deadlines() -> None:
    from lockstep.runtime.effects.descriptors import (
        effective_effect_deadline,
        parse_effect_descriptor,
        parse_scope_result,
    )

    now = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
    descriptor = parse_effect_descriptor(
        managed_descriptor(deadline_seconds=900, scope_state_keys=["outer", "inner"])
    )
    scopes = tuple(
        parse_scope_result(
            {
                "schema": "lockstep.scope-result/v1",
                "effect_id": name,
                "outcome": "PASS",
                "scope_kind": "parallel",
                "scope_digest": digest * 64,
                "absolute_deadline": deadline,
            }
        )
        for name, digest, deadline in (
            ("outer", "a", "2026-08-20T10:10:00+00:00"),
            ("inner", "b", "2026-08-20T10:05:00+00:00"),
        )
    )

    assert effective_effect_deadline(descriptor, now=now, scopes=scopes) == datetime(
        2026, 8, 20, 10, 5, tzinfo=timezone.utc
    )

    with pytest.raises(ValueError, match="scope count"):
        effective_effect_deadline(descriptor, now=now, scopes=scopes[:1])


def test_effect_result_is_closed_bounded_and_bound_to_effect() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_result

    result = parse_effect_result(
        {
            "schema": "lockstep.effect-result/v1",
            "effect_id": "effect-1",
            "outcome": "PASS",
            "result_ref": "blob:" + "a" * 64,
            "artifact_refs": ["artifact:review"],
            "snapshot_ref": "snapshot:" + "b" * 64,
            "diff_ref": "blob:" + "c" * 64,
            "fixed_error_code": None,
            "evidence_refs": ["blob:" + "d" * 64],
        }
    )
    assert result.effect_id == "effect-1"
    with pytest.raises(ValueError, match="unknown"):
        parse_effect_result({**result.to_dict(), "route": "done"})
    with pytest.raises(ValueError, match="fixed_error_code"):
        parse_effect_result(
            {
                **result.to_dict(),
                "outcome": "ERROR",
                "fixed_error_code": "please_route_to_success",
            }
        )


def test_coordinate_identity_is_domain_separated_and_exact() -> None:
    from lockstep.runtime.effects.descriptors import derive_effect_id
    from lockstep.runtime.native_models import NativeCoordinate

    coordinate = NativeCoordinate("thread", "checkpoint", "ns", "task", "interrupt")
    effect_id = derive_effect_id(coordinate, "a" * 64)

    assert (
        effect_id
        == "eff_59a1a0b4200c9375d91e6c13ae5e89db48324dabca315e9e3b5ed79b06027f9d"
    )
    assert effect_id != derive_effect_id(
        NativeCoordinate("thread", "checkpoint-2", "ns", "task", "interrupt"),
        "a" * 64,
    )
    with pytest.raises(ValueError, match="digest"):
        derive_effect_id(coordinate, "not-a-digest")


def test_runtime_input_selector_is_a_closed_owned_union() -> None:
    from lockstep.runtime.effects.descriptors import parse_effect_descriptor
    from lockstep.runtime.effects.models import RuntimeInputSelector

    parsed = parse_effect_descriptor(
        managed_descriptor(inputs={"snapshot": {"runtime_key": "current_project_snapshot"}})
    )
    assert parsed.inputs == (("snapshot", RuntimeInputSelector("current_project_snapshot")),)
    for selector in (
        {"runtime_key": "caller_snapshot"},
        {"runtime_key": "current_project_snapshot", "state_key": "shadow"},
        {"project_snapshot": "current"},
    ):
        with pytest.raises(ValueError, match="selector"):
            parse_effect_descriptor(managed_descriptor(inputs={"snapshot": selector}))


def test_decision_and_acceptance_results_are_exact_closed_variants() -> None:
    from lockstep.runtime.effects.descriptors import (
        parse_acceptance_result,
        parse_decision_result,
        parse_effect_descriptor,
    )

    decision = parse_effect_descriptor({
        "schema": "lockstep.effect/v1", "kind": "decide", "logical_id": "risk",
        "decision": {"type": "changed-paths", "since": "start", "cases": [
            {"label": "high", "paths": ["auth/**"]}
        ], "default": "low"},
        "inputs": {
            "start_snapshot": {"runtime_key": "run_start_project_snapshot"},
            "current_snapshot": {"runtime_key": "current_project_snapshot"},
        },
        "result_schema": "lockstep.decision-result/v1",
    })
    result = parse_decision_result({
        "schema": "lockstep.decision-result/v1", "effect_id": "effect-1",
        "outcome": "PASS", "decision_digest": decision.digest, "value": "high",
    }, descriptor=decision)
    assert result.value == "high"
    with pytest.raises(ValueError, match="outside"):
        parse_decision_result({**result.to_dict(), "value": "injected"}, descriptor=decision)
    with pytest.raises(ValueError, match="digest"):
        parse_decision_result(
            {**result.to_dict(), "decision_digest": "b" * 64}, descriptor=decision
        )

    accepted = parse_acceptance_result({
        "schema": "lockstep.acceptance-result/v1", "effect_id": "effect-2",
        "outcome": "PASS", "artifact_ref": "artifact-review",
        "artifact_digest": "a" * 64, "consent_ref": "consent-1",
        "approval_generation": 1, "destination": "docs/review.md",
        "transformation": "identity", "audience": "local-project",
        "receipt_digest": "b" * 64,
    })
    assert accepted.consent_ref == "consent-1"
    with pytest.raises(ValueError, match="unknown"):
        parse_acceptance_result({**accepted.to_dict(), "runner": "fake"})
    with pytest.raises(ValueError, match="non-null"):
        parse_acceptance_result({**accepted.to_dict(), "consent_ref": None})
