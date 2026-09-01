"""evidence.py — jsonschema Draft 2020-12 validation.

`validate_evidence(schema, evidence) -> list[str]`: [] means accepted;
non-empty means rejected (collected `e.message` strings). `schema is None`
requires a non-empty evidence dict — this is a rule about the ABSENCE of a
schema, not a blanket "empty dict always fails": a schema that declares no
`required` fields tolerates an empty dict just fine.
"""

from lockstep.runtime.evidence import validate_evidence


def test_no_schema_empty_dict_rejected():
    errors = validate_evidence(None, {})
    assert errors


def test_no_schema_nonempty_dict_accepted():
    assert validate_evidence(None, {"path": "a.md"}) == []


def test_schema_missing_required_field_rejected():
    schema = {
        "required": ["path"],
        "properties": {"path": {"type": "string", "pattern": "^[a-z0-9_./-]+$"}},
    }
    errors = validate_evidence(schema, {})
    assert errors
    assert any("path" in e for e in errors)


def test_schema_pattern_mismatch_rejected():
    schema = {
        "required": ["path"],
        "properties": {"path": {"type": "string", "pattern": "^[a-z0-9_./-]+$"}},
    }
    errors = validate_evidence(schema, {"path": "UPPER CASE!"})
    assert errors


def test_schema_valid_evidence_accepted():
    schema = {
        "required": ["path"],
        "properties": {"path": {"type": "string", "pattern": "^[a-z0-9_./-]+$"}},
    }
    assert validate_evidence(schema, {"path": "a/b.md"}) == []


def test_schema_without_required_tolerates_empty_dict():
    schema = {"properties": {"path": {"type": "string"}}}
    assert validate_evidence(schema, {}) == []


def test_schema_wrong_type_rejected():
    schema = {"properties": {"path": {"type": "string"}}}
    errors = validate_evidence(schema, {"path": 123})
    assert errors


def test_schema_multiple_violations_all_collected():
    schema = {
        "required": ["a", "b"],
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    errors = validate_evidence(schema, {})
    # both missing-required violations show up somewhere in the messages
    assert any("a" in e for e in errors)
    assert any("b" in e for e in errors)
