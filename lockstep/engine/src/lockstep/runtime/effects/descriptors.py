"""Closed parsing and canonical identities for protected interrupts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from lockstep.runtime.effects.models import (
    AcceptanceResult,
    AcceptDescriptor,
    ArtifactDescriptor,
    DecisionCase,
    DecisionDescriptor,
    DecisionResult,
    DecisionSpec,
    EffectDescriptor,
    EffectResult,
    ManualParallelContract,
    PublishDescriptor,
    PublishItem,
    RunnerDescriptor,
    RuntimeInputSelector,
    ScopeDescriptor,
    ScopeResult,
    StateSelector,
)
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.payload_limits import PayloadLimitExceeded, bounded_json

EFFECT_KINDS = frozenset(
    {"managed", "manual", "pinned", "verify", "decide", "accept", "publish"}
)
CAPABILITIES = frozenset(
    {
        "workspace",
        "bounded_result",
        "result_stability",
        "sandbox",
        "network",
        "credentials",
        "publication",
    }
)
EFFECT_ERROR_CODES = frozenset(
    {
        "cancelled",
        "deadline_timeout",
        "launch_indeterminate",
        "manifest_invalid",
        "prelaunch_failed",
        "provider_error",
        "result_invalid",
        "runner_failed",
        "sandbox_invalid",
        "terminal_safety_unproved",
        "writes_invalid",
    }
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_GLOB_CHARS = frozenset("*?[]{}")


def _bounded_mapping(value: object, label: str) -> dict[str, Any]:
    try:
        detached = bounded_json(value, label=label)
    except PayloadLimitExceeded as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(detached, dict):
        raise TypeError(f"{label} must be a JSON object")
    return detached


def _closed(
    value: Mapping[str, Any], allowed: set[str], required: set[str], label: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown keys: {sorted(unknown)}")
    missing = required - set(value)
    if missing:
        raise ValueError(f"{label} is missing keys: {sorted(missing)}")


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{label} must be a bounded logical name")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be null or a bounded non-empty string")
    return value


def _positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0 or value > 31_536_000:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _digest(value: object) -> str:
    encoded = _canonical(value)
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _hex_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _string_list(value: object, label: str, *, names: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    parsed = tuple(
        _name(item, label) if names else _optional_string(item, label) for item in value
    )
    if any(item is None for item in parsed):
        raise ValueError(f"{label} values must not be null")
    result = tuple(item for item in parsed if item is not None)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _write_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("write path must be a non-empty string")
    if "\x00" in value:
        raise ValueError("write path may not contain NUL")
    if "\\" in value or any(char in value for char in _GLOB_CHARS):
        raise ValueError("write path must use literal POSIX-relative syntax")
    directory = value.endswith("/")
    body = value[:-1] if directory else value
    if not body or body.startswith("/") or "//" in body:
        raise ValueError("write path must be safe POSIX-relative")
    raw_parts = body.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("write path must be safe POSIX-relative")
    path = PurePosixPath(body)
    if path.is_absolute() or ".git" in path.parts:
        raise ValueError("write path may not escape or name Git controls")
    return body + ("/" if directory else "")


def _runner(value: object) -> RunnerDescriptor | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("runner must be null or an object")
    _closed(
        value,
        {"selector", "required_capabilities"},
        {"selector", "required_capabilities"},
        "runner",
    )
    capabilities = _string_list(
        value["required_capabilities"], "runner capabilities", names=True
    )
    unknown = set(capabilities) - CAPABILITIES
    if unknown:
        raise ValueError(f"runner has unknown capabilities: {sorted(unknown)}")
    return RunnerDescriptor(_name(value["selector"], "runner selector"), capabilities)


def _inputs(value: object) -> tuple[tuple[str, StateSelector | RuntimeInputSelector], ...]:
    if not isinstance(value, dict):
        raise TypeError("inputs must be an object")
    parsed: list[tuple[str, StateSelector | RuntimeInputSelector]] = []
    for input_name, selector in value.items():
        name = _name(input_name, "input name")
        if not isinstance(selector, dict):
            raise TypeError("input selector must be an object")
        if set(selector) == {"state_key"}:
            parsed.append((name, StateSelector(_name(selector["state_key"], "state selector"))))
        elif set(selector) == {"runtime_key"} and selector["runtime_key"] in {
            "run_start_project_snapshot", "current_project_snapshot"
        }:
            parsed.append((name, RuntimeInputSelector(selector["runtime_key"])))
        else:
            raise ValueError("unknown or invalid input selector")
    return tuple(parsed)


def _artifacts(value: object) -> tuple[ArtifactDescriptor, ...]:
    if not isinstance(value, list):
        raise TypeError("artifacts must be an array")
    if len(value) > 32:
        raise ValueError("artifacts exceed the declaration limit")
    parsed = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("artifact must be an object")
        _closed(
            item,
            {"name", "source_path", "media_type", "required"},
            {"name", "source_path", "media_type", "required"},
            "artifact",
        )
        media_type = item["media_type"]
        if not isinstance(media_type, str) or not _MEDIA_TYPE.fullmatch(media_type):
            raise ValueError("artifact media_type is invalid")
        if type(item["required"]) is not bool:
            raise ValueError("artifact required must be a boolean")
        try:
            source_path = _write_path(item["source_path"])
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact source_path must be a safe project file") from exc
        if source_path.endswith("/"):
            raise ValueError("artifact source_path must be an exact file")
        parsed.append(
            ArtifactDescriptor(
                _name(item["name"], "artifact name"),
                source_path,
                media_type,
                item["required"],
            )
        )
    if len({item.name for item in parsed}) != len(parsed):
        raise ValueError("artifact names must be unique")
    return tuple(parsed)


def parse_manual_parallel(value: object) -> ManualParallelContract:
    """Parse the digest-bound aggregate without replacing a step's own writes."""
    raw = _bounded_mapping(value, "manual parallel contract")
    keys = {"id", "branch", "writes"}
    _closed(raw, keys, keys, "manual parallel contract")
    writes = tuple(
        _write_path(item) for item in _string_list(raw["writes"], "parallel writes")
    )
    return ManualParallelContract(
        _name(raw["id"], "parallel id"),
        _name(raw["branch"], "parallel branch"),
        writes,
    )


def parse_effect_descriptor(
    value: object,
    *,
    expected_digest: str | None = None,
    known_state_keys: AbstractSet[str] | None = None,
) -> EffectDescriptor | ScopeDescriptor | DecisionDescriptor | AcceptDescriptor | PublishDescriptor:
    raw = _bounded_mapping(value, "effect descriptor")
    if raw.get("schema") != "lockstep.effect/v1":
        raise ValueError("unsupported effect descriptor schema")
    kind = raw.get("kind")
    if kind == "scope":
        parsed_scope = _parse_scope_descriptor(raw)
        _verify_expected_digest(parsed_scope.digest, expected_digest)
        _verify_known_state_keys(
            (
                *parsed_scope.ancestor_deadline_state_keys,
                parsed_scope.result_state_key,
            ),
            known_state_keys,
        )
        return parsed_scope
    if kind == "decide":
        parsed_decision = _parse_decision_descriptor(raw)
        _verify_expected_digest(parsed_decision.digest, expected_digest)
        return parsed_decision
    if kind == "accept":
        parsed_accept = _parse_accept_descriptor(raw)
        _verify_expected_digest(parsed_accept.digest, expected_digest)
        return parsed_accept
    if kind == "publish":
        parsed_publish = _parse_publish_descriptor(raw)
        _verify_expected_digest(parsed_publish.digest, expected_digest)
        _verify_known_state_keys(
            tuple(
                key
                for item in parsed_publish.items
                for key in (
                    item.producer_result_state_key,
                    item.acceptance_result_state_key,
                )
            ),
            known_state_keys,
        )
        return parsed_publish
    if kind not in EFFECT_KINDS:
        raise ValueError("unknown effect kind")
    allowed = {
        "schema",
        "kind",
        "logical_id",
        "runner",
        "inputs",
        "writes",
        "artifacts",
        "deadline_seconds",
        "scope_state_keys",
        "result_schema",
        "parallel",
    }
    required = allowed - {"scope_state_keys", "parallel"}
    _closed(raw, allowed, required, "effect descriptor")
    runner = _runner(raw["runner"])
    if kind != "manual" and runner is None:
        raise ValueError(f"{kind} effect requires a runner")
    deadline = _positive_int(raw["deadline_seconds"], "deadline_seconds")
    scopes = _string_list(
        raw.get("scope_state_keys", []), "scope_state_keys", names=True
    )
    if kind == "manual" and deadline is not None:
        raise ValueError("unmanaged manual effect may not declare a deadline")
    if kind == "manual" and scopes:
        raise ValueError("unmanaged manual effect may not join a bounded scope")
    if raw["result_schema"] != "lockstep.effect-result/v1":
        raise ValueError("unsupported result_schema")
    writes_raw = raw["writes"]
    if not isinstance(writes_raw, list):
        raise TypeError("writes must be an array")
    writes = tuple(_write_path(item) for item in writes_raw)
    if len(set(writes)) != len(writes):
        raise ValueError("writes must not contain duplicates")
    parallel = parse_manual_parallel(raw["parallel"]) if "parallel" in raw else None
    if parallel is not None:
        if kind != "manual" or runner is not None:
            raise ValueError("parallel write contract requires an unmanaged manual effect")
        if any(write not in parallel.writes for write in writes):
            raise ValueError("parallel writes must include every declared manual write")
    artifacts = _artifacts(raw["artifacts"])
    for artifact in artifacts:
        if artifact.source_path.endswith("/") or not any(
            artifact.source_path == declared
            or (declared.endswith("/") and artifact.source_path.startswith(declared))
            for declared in writes
        ):
            raise ValueError(
                "artifact source_path must be an exact file covered by declared writes"
            )
    canonical = _canonical(raw)
    parsed = EffectDescriptor(
        schema=raw["schema"],
        kind=kind,
        logical_id=_name(raw["logical_id"], "logical_id"),
        runner=runner,
        inputs=_inputs(raw["inputs"]),
        writes=writes,
        artifacts=artifacts,
        deadline_seconds=deadline,
        scope_state_keys=scopes,
        result_schema=raw["result_schema"],
        canonical_json=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
        parallel=parallel,
    )
    _verify_expected_digest(parsed.digest, expected_digest)
    _verify_known_state_keys(
        (
            *(selector.state_key for _name, selector in parsed.inputs if isinstance(selector, StateSelector)),
            *parsed.scope_state_keys,
        ),
        known_state_keys,
    )
    return parsed


def _parse_decision_descriptor(raw: dict[str, Any]) -> DecisionDescriptor:
    allowed = {"schema", "kind", "logical_id", "decision", "inputs", "result_schema"}
    _closed(raw, allowed, allowed, "decision descriptor")
    decision = raw["decision"]
    if not isinstance(decision, dict):
        raise TypeError("decision must be an object")
    _closed(decision, {"type", "since", "cases", "default"}, {"type", "since", "cases", "default"}, "decision")
    if decision["type"] != "changed-paths" or decision["since"] != "start":
        raise ValueError("unsupported decision strategy")
    cases = decision["cases"]
    if not isinstance(cases, list):
        raise TypeError("decision cases must be an array")
    labels: list[str] = []
    normalized_cases: list[DecisionCase] = []
    for case in cases:
        if not isinstance(case, dict):
            raise TypeError("decision case must be an object")
        _closed(case, {"label", "paths"}, {"label", "paths"}, "decision case")
        label = _name(case["label"], "decision label")
        paths = _string_list(case["paths"], "decision paths")
        if not paths:
            raise ValueError("decision paths must not be empty")
        labels.append(label)
        normalized_cases.append(DecisionCase(label, paths))
    default = _name(decision["default"], "decision default")
    if len(set((*labels, default))) != len(labels) + 1:
        raise ValueError("decision labels and default must be unique")
    inputs = _inputs(raw["inputs"])
    expected_inputs = {
        "start_snapshot": "run_start_project_snapshot",
        "current_snapshot": "current_project_snapshot",
    }
    if {name: getattr(selector, "runtime_key", None) for name, selector in inputs} != expected_inputs:
        raise ValueError("decision inputs must be exact runtime snapshot selectors")
    if raw["result_schema"] != "lockstep.decision-result/v1":
        raise ValueError("unsupported decision result_schema")
    canonical = _canonical(raw)
    return DecisionDescriptor(
        raw["schema"], "decide", _name(raw["logical_id"], "logical_id"),
        DecisionSpec("changed-paths", "start", tuple(normalized_cases), default),
        tuple((name, selector) for name, selector in inputs if isinstance(selector, RuntimeInputSelector)),
        raw["result_schema"], canonical, hashlib.sha256(canonical).hexdigest(),
    )


def _parse_accept_descriptor(raw: dict[str, Any]) -> AcceptDescriptor:
    allowed = {
        "schema", "kind", "logical_id", "artifact_handle",
        "producer_result_state_key", "declared_name", "destination",
        "transformation", "audience", "verdict", "result_schema",
    }
    _closed(raw, allowed, allowed, "accept descriptor")
    if raw["verdict"] != "PASS":
        raise ValueError("accept verdict must be PASS")
    if raw["result_schema"] != "lockstep.acceptance-result/v1":
        raise ValueError("unsupported accept result_schema")
    destination = _write_path(raw["destination"])
    if destination.endswith("/"):
        raise ValueError("accept destination must be an exact file")
    if raw["transformation"] != "identity":
        raise ValueError("accept transformation must be identity")
    if raw["audience"] != "local-project":
        raise ValueError("accept audience must be local-project")
    canonical = _canonical(raw)
    return AcceptDescriptor(
        raw["schema"],
        "accept",
        _name(raw["logical_id"], "logical_id"),
        _name(raw["artifact_handle"], "artifact_handle"),
        _name(raw["producer_result_state_key"], "producer result state key"),
        _name(raw["declared_name"], "declared artifact name"),
        destination,
        "identity",
        "local-project",
        "PASS",
        raw["result_schema"],
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def _parse_publish_descriptor(raw: dict[str, Any]) -> PublishDescriptor:
    allowed = {"schema", "kind", "logical_id", "items", "result_schema"}
    _closed(raw, allowed, allowed, "publish descriptor")
    if raw["result_schema"] != "lockstep.effect-result/v1":
        raise ValueError("unsupported publish result_schema")
    items = raw["items"]
    if not isinstance(items, list) or not items or len(items) > 32:
        raise ValueError("publish items must be a bounded non-empty array")
    parsed: list[PublishItem] = []
    destinations: list[str] = []
    for item in items:
        fields = {
            "qualified_handle", "producer_result_state_key", "declared_name",
            "acceptance_result_state_key", "destination", "transformation", "audience",
        }
        if not isinstance(item, dict):
            raise TypeError("publish item must be an object")
        _closed(item, fields, fields, "publish item")
        destination = _write_path(item["destination"])
        if destination.endswith("/"):
            raise ValueError("publish destination must be an exact file")
        if item["transformation"] != "identity":
            raise ValueError("publish transformation must be identity")
        if item["audience"] != "local-project":
            raise ValueError("publish audience must be local-project")
        destinations.append(destination)
        parsed.append(
            PublishItem(
                _name(item["qualified_handle"], "qualified artifact handle"),
                _name(item["producer_result_state_key"], "producer result state key"),
                _name(item["declared_name"], "declared artifact name"),
                _name(item["acceptance_result_state_key"], "acceptance result state key"),
                destination,
                "identity",
                "local-project",
            )
        )
    if len(set(destinations)) != len(destinations):
        raise ValueError("publish destinations must be unique")
    canonical = _canonical(raw)
    return PublishDescriptor(
        raw["schema"],
        "publish",
        _name(raw["logical_id"], "logical_id"),
        tuple(parsed),
        raw["result_schema"],
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def _verify_expected_digest(actual: str, expected: str | None) -> None:
    if expected is None:
        return
    _hex_digest(expected, "expected descriptor digest")
    if actual != expected:
        raise ValueError("descriptor digest mismatch")


def _verify_known_state_keys(
    selectors: Sequence[str], known_state_keys: AbstractSet[str] | None
) -> None:
    if known_state_keys is None:
        return
    unknown = set(selectors) - set(known_state_keys)
    if unknown:
        raise ValueError(f"unknown state selector: {sorted(unknown)}")


def _parse_scope_descriptor(raw: dict[str, Any]) -> ScopeDescriptor:
    allowed = {
        "schema",
        "kind",
        "logical_id",
        "scope_kind",
        "duration_seconds",
        "runner_selector",
        "ancestor_deadline_state_keys",
        "result_state_key",
        "result_schema",
    }
    _closed(raw, allowed, allowed, "scope descriptor")
    scope_kind = raw["scope_kind"]
    if scope_kind not in {"call", "parallel"}:
        raise ValueError("unknown scope_kind")
    runner_selector = raw["runner_selector"]
    if scope_kind == "call":
        runner_selector = _name(runner_selector, "runner_selector")
    elif runner_selector is not None:
        raise ValueError("parallel runner_selector must be null")
    if raw["result_schema"] != "lockstep.scope-result/v1":
        raise ValueError("unsupported result_schema")
    canonical = _canonical(raw)
    return ScopeDescriptor(
        schema=raw["schema"],
        kind="scope",
        logical_id=_name(raw["logical_id"], "logical_id"),
        scope_kind=scope_kind,
        duration_seconds=_positive_int(raw["duration_seconds"], "duration_seconds"),
        runner_selector=runner_selector,
        ancestor_deadline_state_keys=_string_list(
            raw["ancestor_deadline_state_keys"],
            "ancestor_deadline_state_keys",
            names=True,
        ),
        result_state_key=_name(raw["result_state_key"], "result_state_key"),
        result_schema=raw["result_schema"],
        canonical_json=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
    )


def parse_effect_result(value: object) -> EffectResult:
    raw = _bounded_mapping(value, "effect result")
    fields = {
        "schema",
        "effect_id",
        "outcome",
        "result_ref",
        "artifact_refs",
        "snapshot_ref",
        "diff_ref",
        "fixed_error_code",
        "evidence_refs",
    }
    _closed(raw, fields, fields, "effect result")
    if raw["schema"] != "lockstep.effect-result/v1":
        raise ValueError("unsupported effect result schema")
    if raw["outcome"] not in {"PASS", "FAIL", "ERROR"}:
        raise ValueError("unknown effect result outcome")
    fixed_error_code = _optional_string(raw["fixed_error_code"], "fixed_error_code")
    if raw["outcome"] == "ERROR" and fixed_error_code is None:
        raise ValueError("ERROR result requires fixed_error_code")
    if fixed_error_code is not None and fixed_error_code not in EFFECT_ERROR_CODES:
        raise ValueError("unknown effect fixed_error_code")
    if raw["outcome"] != "ERROR" and fixed_error_code is not None:
        raise ValueError("non-ERROR result may not contain fixed_error_code")
    canonical = _canonical(raw)
    return EffectResult(
        schema=raw["schema"],
        effect_id=_name(raw["effect_id"], "effect_id"),
        outcome=raw["outcome"],
        result_ref=_optional_string(raw["result_ref"], "result_ref"),
        artifact_refs=_string_list(raw["artifact_refs"], "artifact_refs"),
        snapshot_ref=_optional_string(raw["snapshot_ref"], "snapshot_ref"),
        diff_ref=_optional_string(raw["diff_ref"], "diff_ref"),
        fixed_error_code=fixed_error_code,
        evidence_refs=_string_list(raw["evidence_refs"], "evidence_refs"),
        canonical_json=canonical,
    )


def parse_decision_result(
    value: object, *, descriptor: DecisionDescriptor | None = None
) -> DecisionResult:
    raw = _bounded_mapping(value, "decision result")
    common = {"schema", "effect_id", "outcome", "decision_digest"}
    if raw.get("schema") != "lockstep.decision-result/v1":
        raise ValueError("unsupported decision result schema")
    decision_digest = _hex_digest(raw.get("decision_digest"), "decision_digest")
    if descriptor is not None and decision_digest != descriptor.digest:
        raise ValueError("decision result digest does not match descriptor")
    outcome = raw.get("outcome")
    if outcome == "PASS":
        _closed(raw, common | {"value"}, common | {"value"}, "decision result")
        label = _name(raw["value"], "decision value")
        if descriptor is not None:
            labels = [case.label for case in descriptor.decision.cases]
            labels.append(descriptor.decision.default)
            if label not in labels:
                raise ValueError("decision value is outside the descriptor enum")
        return DecisionResult(
            raw["schema"], _name(raw["effect_id"], "effect_id"), "PASS",
            decision_digest, value=label,
        )
    if outcome == "ERROR":
        _closed(raw, common | {"fixed_error_code"}, common | {"fixed_error_code"}, "decision result")
        if raw["fixed_error_code"] != "decision_input_invalid":
            raise ValueError("unknown decision fixed_error_code")
        return DecisionResult(
            raw["schema"], _name(raw["effect_id"], "effect_id"), "ERROR",
            decision_digest,
            fixed_error_code="decision_input_invalid",
        )
    raise ValueError("unknown decision result outcome")


def parse_acceptance_result(
    value: object, *, descriptor: AcceptDescriptor | None = None
) -> AcceptanceResult:
    raw = _bounded_mapping(value, "acceptance result")
    fields = {
        "schema", "effect_id", "outcome", "artifact_ref", "artifact_digest",
        "destination", "transformation", "audience", "consent_ref",
        "approval_generation", "receipt_digest",
    }
    _closed(raw, fields, fields, "acceptance result")
    if raw["schema"] != "lockstep.acceptance-result/v1" or raw["outcome"] != "PASS":
        raise ValueError("unsupported acceptance result")
    artifact_ref = _optional_string(raw["artifact_ref"], "artifact_ref")
    consent_ref = _optional_string(raw["consent_ref"], "consent_ref")
    if artifact_ref is None or consent_ref is None:
        raise ValueError("acceptance references must be non-null")
    destination = _write_path(raw["destination"])
    if destination.endswith("/"):
        raise ValueError("acceptance destination must be an exact file")
    if raw["transformation"] != "identity":
        raise ValueError("acceptance publication commitment transformation must be identity")
    if raw["audience"] != "local-project":
        raise ValueError("acceptance publication commitment audience must be local-project")
    if descriptor is not None and (
        destination != descriptor.destination
        or raw["transformation"] != descriptor.transformation
        or raw["audience"] != descriptor.audience
    ):
        raise ValueError("acceptance publication commitment differs from descriptor")
    approval_generation = raw["approval_generation"]
    if type(approval_generation) is not int or approval_generation < 0:
        raise ValueError("approval_generation must be a non-negative integer")
    return AcceptanceResult(
        raw["schema"], _name(raw["effect_id"], "effect_id"), "PASS",
        artifact_ref,
        _hex_digest(raw["artifact_digest"], "artifact_digest"),
        destination,
        "identity",
        "local-project",
        consent_ref,
        approval_generation,
        _hex_digest(raw["receipt_digest"], "receipt_digest"),
    )


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_scope_result(value: object) -> ScopeResult:
    raw = _bounded_mapping(value, "scope result")
    common = {"schema", "effect_id", "outcome", "scope_kind", "scope_digest"}
    if raw.get("schema") != "lockstep.scope-result/v1":
        raise ValueError("unsupported scope result schema")
    if raw.get("scope_kind") not in {"call", "parallel"}:
        raise ValueError("unknown scope_kind")
    outcome = raw.get("outcome")
    if outcome == "ERROR":
        fields = common | {"fixed_error_code"}
        _closed(raw, fields, fields, "scope result; mixed variants are forbidden")
        if raw["fixed_error_code"] != "scope_timeout":
            raise ValueError("unknown scope fixed_error_code")
        return ScopeResult(
            raw["schema"],
            _name(raw["effect_id"], "effect_id"),
            "ERROR",
            raw["scope_kind"],
            _hex_digest(raw["scope_digest"], "scope_digest"),
            fixed_error_code="scope_timeout",
        )
    if outcome != "PASS":
        raise ValueError("unknown scope result outcome")
    fields = common | {"absolute_deadline"}
    if raw["scope_kind"] == "call":
        fields |= {"runner_selector", "runner_binding_digest"}
    _closed(raw, fields, fields, "scope result; mixed variants are forbidden")
    deadline = (
        None
        if raw["absolute_deadline"] is None
        else _parse_datetime(raw["absolute_deadline"], "absolute_deadline")
    )
    runner_selector = None
    runner_digest = None
    if raw["scope_kind"] == "call":
        runner_selector = _name(raw["runner_selector"], "runner_selector")
        runner_digest = _hex_digest(
            raw["runner_binding_digest"], "runner_binding_digest"
        )
    return ScopeResult(
        raw["schema"],
        _name(raw["effect_id"], "effect_id"),
        "PASS",
        raw["scope_kind"],
        _hex_digest(raw["scope_digest"], "scope_digest"),
        deadline,
        runner_selector,
        runner_digest,
    )


def _trusted_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(timezone.utc)


def build_scope_result(
    *,
    effect_id: str,
    scope_digest: str,
    scope_kind: str,
    now: datetime,
    duration_seconds: int | None,
    ancestors: Sequence[ScopeResult],
    runner_selector: str | None = None,
    runner_binding_digest: str | None = None,
) -> ScopeResult:
    now = _trusted_utc(now, "now")
    deadlines: list[datetime] = []
    duration = _positive_int(duration_seconds, "duration_seconds")
    if duration is not None:
        deadlines.append(now + timedelta(seconds=duration))
    for ancestor in ancestors:
        if ancestor.outcome == "ERROR" or (
            ancestor.absolute_deadline is not None and ancestor.absolute_deadline <= now
        ):
            return ScopeResult(
                "lockstep.scope-result/v1",
                _name(effect_id, "effect_id"),
                "ERROR",
                scope_kind,
                _hex_digest(scope_digest, "scope_digest"),
                fixed_error_code="scope_timeout",
            )
        if ancestor.absolute_deadline is not None:
            deadlines.append(ancestor.absolute_deadline)
    payload: dict[str, Any] = {
        "schema": "lockstep.scope-result/v1",
        "effect_id": effect_id,
        "outcome": "PASS",
        "scope_kind": scope_kind,
        "scope_digest": scope_digest,
        "absolute_deadline": min(deadlines).isoformat() if deadlines else None,
    }
    if scope_kind == "call":
        payload["runner_selector"] = runner_selector
        payload["runner_binding_digest"] = runner_binding_digest
    return parse_scope_result(payload)


def effective_effect_deadline(
    descriptor: EffectDescriptor,
    *,
    now: datetime,
    scopes: Sequence[ScopeResult],
) -> datetime | None:
    """Resolve the immutable minimum bound for one ordinary effect request."""

    if descriptor.kind == "manual" and (descriptor.deadline_seconds or scopes):
        raise ValueError("unmanaged manual effect may not have a bounded scope")
    if len(scopes) != len(descriptor.scope_state_keys):
        raise ValueError("scope count does not match descriptor scope selectors")
    current = _trusted_utc(now, "now")
    candidates: list[datetime] = []
    if descriptor.deadline_seconds is not None:
        candidates.append(current + timedelta(seconds=descriptor.deadline_seconds))
    for scope in scopes:
        if scope.outcome == "ERROR":
            raise ValueError("ancestor scope is timed out")
        if scope.absolute_deadline is not None:
            if scope.absolute_deadline <= current:
                raise ValueError("ancestor scope is timed out")
            candidates.append(scope.absolute_deadline)
    return min(candidates) if candidates else None


def derive_effect_id(coordinate: NativeCoordinate, descriptor_digest: str) -> str:
    digest = _hex_digest(descriptor_digest, "descriptor digest")
    identity = [
        coordinate.thread_id,
        coordinate.checkpoint_ns,
        coordinate.checkpoint_id,
        coordinate.task_id,
        coordinate.interrupt_id,
        digest,
    ]
    encoded = b"lockstep.effect-id/v1\0" + _canonical(identity)
    return "eff_" + hashlib.sha256(encoded).hexdigest()
