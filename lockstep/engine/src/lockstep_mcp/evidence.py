"""Evidence schema validation (Task 4).

`validate_evidence` is the deterministic gate on what an agent's reported
evidence dict must look like: jsonschema Draft 2020-12, `[]` means the
evidence is accepted. `schema is None` (no `evidence_schema` declared on
the step) falls back to "evidence must be a non-empty dict" — a step with
no schema still has to report SOMETHING. A declared schema that itself
requires nothing tolerates an empty dict; the no-schema rule is about the
absence of a schema, not a blanket rejection of `{}`.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator


def validate_evidence(schema: dict | None, evidence: dict) -> list[str]:
    if schema is None:
        if not evidence:
            return ["evidence must be a non-empty dict when the step declares no evidence_schema"]
        return []
    validator = Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(evidence)]
