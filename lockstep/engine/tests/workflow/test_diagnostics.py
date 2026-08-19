from __future__ import annotations

import json
from pathlib import Path

import pytest

from lockstep.workflow.diagnostics import Diagnostic, DiagnosticError
from lockstep.workflow.schema import load_workflow, parse_workflow


def test_diagnostic_renders_stable_text_and_json() -> None:
    diagnostic = Diagnostic(
        code="LSW203",
        message="invalid retry exit",
        path=Path(".lockstep/workflows/release.workflow.yaml"),
        line=18,
        column=5,
        pointer="/flow/0/retry/exhausted",
        hint="'exhausted' must be 'escalate' or a declared structured handler.",
        generated_node="validate-plan",
    )

    assert diagnostic.render_text() == """LSW203 invalid retry exit

.lockstep/workflows/release.workflow.yaml:18:5

DSL pointer: /flow/0/retry/exhausted
'exhausted' must be 'escalate' or a declared structured handler.
Generated node: validate-plan"""
    assert json.loads(diagnostic.render_json()) == {
        "code": "LSW203",
        "message": "invalid retry exit",
        "path": ".lockstep/workflows/release.workflow.yaml",
        "line": 18,
        "column": 5,
        "pointer": "/flow/0/retry/exhausted",
        "hint": "'exhausted' must be 'escalate' or a declared structured handler.",
        "generated_node": "validate-plan",
    }


def test_diagnostic_error_renders_all_diagnostics() -> None:
    error = DiagnosticError(
        (
            Diagnostic("LSW105", "unknown key", Path("one.yaml"), 1, 1, "/one", "remove it"),
            Diagnostic("LSW106", "missing key", Path("one.yaml"), 2, 1, "", "add it"),
        )
    )

    assert str(error) == "LSW105 unknown key\n\none.yaml:1:1\n\nDSL pointer: /one\nremove it\n\nLSW106 missing key\n\none.yaml:2:1\n\nDSL pointer: \nadd it"
    assert json.loads(error.render_json()) == [
        {"code": "LSW105", "message": "unknown key", "path": "one.yaml", "line": 1, "column": 1, "pointer": "/one", "hint": "remove it", "generated_node": None},
        {"code": "LSW106", "message": "missing key", "path": "one.yaml", "line": 2, "column": 1, "pointer": "", "hint": "add it", "generated_node": None},
    ]


def test_syntax_diagnostic_contains_source_mark(tmp_path: Path) -> None:
    source = tmp_path / "release.workflow.yaml"
    source.write_text("workflow_version: [\n")

    with pytest.raises(DiagnosticError) as raised:
        parse_workflow(load_workflow(source))

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "LSW101"
    assert (diagnostic.line, diagnostic.column) == (2, 1)
