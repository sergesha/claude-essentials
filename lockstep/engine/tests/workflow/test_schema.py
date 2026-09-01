from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from lockstep.workflow.diagnostics import DiagnosticError
from lockstep.workflow.schema import load_workflow, load_workflow_bytes, parse_workflow

BASE = '''\
workflow_version: "1"
name: release
description: Release the project safely
protect: ["**"]
x-owner-note: ignored
'''


@pytest.fixture
def workflow_file(tmp_path: Path) -> Path:
    return tmp_path / "release.workflow.yaml"


def raises_diagnostic(prefix: str, workflow_file: Path):
    with pytest.raises(DiagnosticError) as raised:
        parse_workflow(load_workflow(workflow_file))
    return next(diagnostic for diagnostic in raised.value.diagnostics if diagnostic.code.startswith(prefix))


def test_block_union_rejects_two_discriminators(workflow_file: Path) -> None:
    workflow_file.write_text(BASE + "flow:\n- step: plan\n  verify: {command: pytest}\n")

    err = raises_diagnostic("LSW1", workflow_file)

    assert (err.line, err.pointer) == (7, "/flow/0")
    assert err.message == "a flow item must contain exactly one block discriminator"


def test_v1_requires_full_protection(workflow_file: Path) -> None:
    workflow_file.write_text(BASE.replace('["**"]', '["src/**"]') + "flow: []\n")

    assert raises_diagnostic("LSW3", workflow_file).hint == 'use protect: ["**"]'


def test_loader_retains_mapping_and_list_start_marks(workflow_file: Path) -> None:
    workflow_file.write_text(BASE + "flow:\n- verify:\n    command: pytest -q\n")

    document = load_workflow(workflow_file)

    assert (document.mark_for("/flow").line, document.mark_for("/flow").column) == (7, 1)
    assert (document.mark_for("/flow/0").line, document.mark_for("/flow/0").column) == (7, 3)
    assert (document.mark_for("/flow/0/verify").line, document.mark_for("/flow/0/verify").column) == (8, 5)


@pytest.mark.parametrize(
    ("source", "code", "pointer"),
    [
        (BASE + "flow: []\nunknown: true\n", "LSW105", "/unknown"),
        (BASE.replace("name: release", "name: Release") + "flow: []\n", "LSW110", "/name"),
        (BASE.replace("name: release", "name: other") + "flow: []\n", "LSW109", "/name"),
        (BASE.replace("description: Release the project safely\n", "") + "flow: []\n", "LSW106", ""),
    ],
)
def test_document_schema_rejects_invalid_fields(
    workflow_file: Path, source: str, code: str, pointer: str
) -> None:
    workflow_file.write_text(source)

    error = raises_diagnostic(code, workflow_file)

    assert error.pointer == pointer


def test_loader_rejects_aliases_and_duplicate_keys(workflow_file: Path) -> None:
    workflow_file.write_text(BASE + "flow: &empty []\nagain: *empty\n")
    alias = raises_diagnostic("LSW102", workflow_file)
    assert (alias.line, alias.column) == (7, 8)

    workflow_file.write_text(BASE + "flow: []\nname: release\n")
    duplicate = raises_diagnostic("LSW103", workflow_file)
    assert duplicate.pointer == "/name"


def test_loader_reports_the_nested_pointer_for_an_alias(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE + "flow:\n- verify: {command: &command pytest -q, cwd: *command}\n"
    )

    alias = raises_diagnostic("LSW102", workflow_file)

    assert alias.pointer == "/flow/0/verify/cwd"


def _structure_error(workflow_file: Path, source: bytes):
    with pytest.raises(DiagnosticError) as raised:
        load_workflow_bytes(workflow_file, source)
    error = raised.value.diagnostics[0]
    assert error.code == "LSW111"
    assert error.pointer == ""
    assert error.line is not None and error.column is not None
    assert "reduce" in error.hint.lower()
    return error


def test_loader_rejects_yaml_deeper_than_sixty_four_levels(
    workflow_file: Path,
) -> None:
    error = _structure_error(workflow_file, b"- " * 65 + b"leaf\n")

    assert "depth" in error.message


def test_loader_rejects_more_than_fifty_thousand_yaml_nodes(
    workflow_file: Path,
) -> None:
    nested = "".join(
        "-\n" + "".join(f"  - n-{group}-{index}\n" for index in range(9_000))
        for group in range(6)
    ).encode()

    error = _structure_error(workflow_file, nested)

    assert "node" in error.message


def test_loader_rejects_more_than_ten_thousand_items_in_one_collection(
    workflow_file: Path,
) -> None:
    error = _structure_error(workflow_file, b"- item\n" * 10_001)

    assert "collection" in error.message


def test_loader_rejects_more_than_two_mib_of_aggregate_scalar_bytes(
    workflow_file: Path,
) -> None:
    error = _structure_error(workflow_file, b"value: " + b"x" * (2 * 1024 * 1024 + 1) + b"\n")

    assert "scalar" in error.message


def test_include_graph_accepts_the_documented_unquoted_on_key(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE
        + '''\
flow:
- include_graph:
    id: approval-fragment
    path: .lockstep/fragments/release-approval.graph.yaml
    on:
      pass: next
      fail: escalate
      error: escalate
'''
    )

    workflow = parse_workflow(load_workflow(workflow_file))

    assert workflow.flow[0].on == {"pass": "next", "fail": "escalate", "error": "escalate"}


@pytest.mark.parametrize("outcome", ["fail", "error"])
def test_include_graph_rejects_arbitrary_failure_handler_values(
    workflow_file: Path, outcome: str
) -> None:
    workflow_file.write_text(
        BASE
        + f'''\
flow:
- include_graph:
    id: approval-fragment
    path: .lockstep/fragments/release-approval.graph.yaml
    on: {{pass: next, {outcome}: typo}}
'''
    )

    assert raises_diagnostic("LSW108", workflow_file).pointer == f"/flow/0/include_graph/on/{outcome}"


def test_include_graph_accepts_exact_failure_routing_tokens(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE
        + '''\
flow:
- include_graph:
    id: approval-fragment
    path: .lockstep/fragments/release-approval.graph.yaml
    on: {pass: next, fail: escalate, error: escalate}
'''
    )

    assert parse_workflow(load_workflow(workflow_file)).flow[0].on == {
        "pass": "next", "fail": "escalate", "error": "escalate"
    }


def test_include_graph_defaults_omitted_failure_routing_to_escalate(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE + "flow:\n- include_graph: {id: approval, path: f.graph.yaml}\n"
    )

    assert parse_workflow(load_workflow(workflow_file)).flow[0].on == {
        "pass": "next", "fail": "escalate", "error": "escalate"
    }


@pytest.mark.parametrize("outcome", ["fail", "error"])
def test_include_graph_rejects_explicit_null_failure_routing(
    workflow_file: Path, outcome: str
) -> None:
    workflow_file.write_text(
        BASE
        + f'''\
flow:
- include_graph:
    id: approval-fragment
    path: .lockstep/fragments/release-approval.graph.yaml
    on: {{pass: next, {outcome}: null}}
'''
    )

    assert raises_diagnostic("LSW108", workflow_file).pointer == f"/flow/0/include_graph/on/{outcome}"


def test_ir_recursively_freezes_mapping_fields_and_retains_defaults(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE
        + '''\
defaults:
  retry: {limit: 2, exhausted: escalate}
flow:
- step: plan
  task: Make a plan
  exit: A plan exists
  evidence: {answer: {type: string}}
- decide:
    using: {type: changed-paths, since: start, cases: {high: [src/**]}, default: low}
'''
    )

    workflow = parse_workflow(load_workflow(workflow_file))

    assert workflow.defaults.retry.limit == 2
    assert isinstance(workflow.flow[0].evidence, MappingProxyType)
    with pytest.raises(TypeError):
        workflow.flow[0].evidence["answer"] = "changed"
    with pytest.raises(TypeError):
        workflow.flow[0].evidence["answer"]["type"] = "changed"
    with pytest.raises(TypeError):
        workflow.flow[1].using["cases"]["high"] = ()


@pytest.mark.parametrize(
    ("block", "pointer"),
    [
        ("verify: {command: pytest -q}\n  command: ruff check .", "/flow/0/command"),
        ("decide: {using: {type: changed-paths, since: start, cases: {}, default: low}}\n  id: risk", "/flow/0/id"),
    ],
)
def test_mapping_blocks_reject_outer_fields_instead_of_discarding_them(
    workflow_file: Path, block: str, pointer: str
) -> None:
    workflow_file.write_text(BASE + "flow:\n- " + block + "\n")

    assert raises_diagnostic("LSW105", workflow_file).pointer == pointer


@pytest.mark.parametrize(
    ("using", "pointer"),
    [
        ("{type: changed-paths, since: start, cases: {'bad label': [src/**]}, default: low}", "/flow/0/decide/using/cases/bad label"),
        ("{type: changed-paths, since: start, cases: {high: []}, default: low}", "/flow/0/decide/using/cases/high"),
        ("{type: changed-paths, since: start, cases: {high: [src/**]}, default: 'bad label'}", "/flow/0/decide/using/default"),
    ],
)
def test_decide_schema_matches_the_exact_closed_descriptor_domain(
    workflow_file: Path, using: str, pointer: str
) -> None:
    workflow_file.write_text(BASE + f"flow:\n- decide: {{id: risk, using: {using}}}\n")

    assert raises_diagnostic("LSW1", workflow_file).pointer == pointer


@pytest.mark.parametrize(
    ("block", "pointer"),
    [
        ("graph: {fragment: {exits: {pass: done}, effects: {mode: read-only, writes: []}}, nodes: {}, edges: []}", "/flow/0/graph/fragment"),
        ("graph: {fragment: {entry: start, exits: {}, effects: {mode: read-only, writes: []}}, nodes: {}, edges: []}", "/flow/0/graph/fragment/exits"),
        ("include_graph: {id: approval, path: f.graph.yaml, on: {pass: wrong}}", "/flow/0/include_graph/on/pass"),
        ("include_graph: {id: approval, path: f.graph.yaml, on: {pass: next, race: escalate}}", "/flow/0/include_graph/on/race"),
    ],
)
def test_graph_boundaries_validate_their_structural_contract(
    workflow_file: Path, block: str, pointer: str
) -> None:
    workflow_file.write_text(BASE + "flow:\n- " + block + "\n")

    assert raises_diagnostic("LSW1", workflow_file).pointer == pointer


def test_extensions_remain_inert(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE
        + "flow:\n- verify:\n    command: pytest -q\n    x-runner-option: untrusted\n"
    )

    workflow = parse_workflow(load_workflow(workflow_file))

    assert workflow.name == "release"
    assert workflow.flow[0].command == "pytest -q"
    assert not hasattr(workflow.flow[0], "runner_option")


def test_explicit_ids_are_unique_across_nested_structured_flow(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE
        + '''\
flow:
- verify: {id: checks, command: pytest -q}
- choose:
    value: risk
    cases:
      low: [{verify: {id: checks, command: ruff check .}}]
'''
    )

    duplicate = raises_diagnostic("LSW110", workflow_file)

    assert duplicate.pointer == "/flow/1/choose/cases/low/0"
    assert duplicate.message == "duplicate id 'checks'"


def test_call_export_requires_an_explicit_id(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE + "flow:\n- call: {workflow: review, runner: codex, artifacts: {review: .lockstep/review.md}}\n"
    )

    assert raises_diagnostic("LSW106", workflow_file).pointer == "/flow/0/call"


def test_step_artifact_parses_as_the_exact_frozen_export_shape(
    workflow_file: Path,
) -> None:
    workflow_file.write_text(
        BASE
        + '''\
flow:
- step: review
  task: Review the change
  exit: Review is complete
  writes: [review.md]
  artifact:
    handle: review
    path: review.md
    markdown:
      sections: [Findings, Verdict]
'''
    )

    artifact = parse_workflow(load_workflow(workflow_file)).flow[0].artifact

    assert (artifact.handle, artifact.path) == ("review", "review.md")
    assert artifact.markdown.sections == ("Findings", "Verdict")
    with pytest.raises((AttributeError, TypeError)):
        artifact.handle = "other"


@pytest.mark.parametrize(
    ("artifact", "pointer"),
    [
        ("    path: review.md\n    markdown: {sections: [Findings]}\n", "/flow/0/artifact"),
        ("    handle: review\n    markdown: {sections: [Findings]}\n", "/flow/0/artifact"),
        ("    handle: review\n    path: review.md\n", "/flow/0/artifact"),
        ("    handle: review\n    path: review.md\n    markdown: {}\n", "/flow/0/artifact/markdown"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: [Findings]}\n    extra: true\n", "/flow/0/artifact/extra"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: [Findings], extra: true}\n", "/flow/0/artifact/markdown/extra"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: Findings}\n", "/flow/0/artifact/markdown/sections"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: []}\n", "/flow/0/artifact/markdown/sections"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: ['']}\n", "/flow/0/artifact/markdown/sections/0"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: [Findings, Findings]}\n", "/flow/0/artifact/markdown/sections/1"),
        ("    handle: review\n    path: review.md\n    markdown: {sections: [7]}\n", "/flow/0/artifact/markdown/sections/0"),
    ],
)
def test_step_artifact_rejects_non_closed_or_invalid_markdown_shape(
    workflow_file: Path, artifact: str, pointer: str
) -> None:
    workflow_file.write_text(
        BASE
        + "flow:\n"
        + "- step: review\n"
        + "  task: Review the change\n"
        + "  exit: Review is complete\n"
        + "  writes: [review.md]\n"
        + "  artifact:\n"
        + artifact
    )

    assert raises_diagnostic("LSW1", workflow_file).pointer == pointer


def test_artifact_headings_remain_subject_to_the_existing_yaml_budgets(
    workflow_file: Path,
) -> None:
    headings = ", ".join(f"Heading {index}" for index in range(10_001))
    workflow_file.write_text(
        BASE
        + "flow:\n"
        + "- step: review\n"
        + "  task: Review the change\n"
        + "  exit: Review is complete\n"
        + "  writes: [review.md]\n"
        + "  artifact:\n"
        + "    handle: review\n"
        + "    path: review.md\n"
        + f"    markdown: {{sections: [{headings}]}}\n"
    )

    error = raises_diagnostic("LSW111", workflow_file)

    assert "collection" in error.message


def test_core_blocks_parse_into_typed_ir(workflow_file: Path) -> None:
    workflow_file.write_text(
        BASE
        + '''\
flow:
- step: plan
  task: Make a plan
  exit: A plan exists
- verify:
    id: tests
    command: pytest -q
- decide:
    id: risk
    using:
      type: changed-paths
      since: start
      cases: {high: [src/**]}
      default: low
- choose:
    value: risk
    cases:
      low: [{escalate: {}}]
- repeat:
    id: retry-tests
    limit: 2
    until: tests.passed
    do: [{verify: {id: inner, command: pytest -q}}]
    exhausted: escalate
- call:
    id: review
    workflow: independent-review
    runner: codex
- accept:
    artifact_from: review.review
    verdict: PASS
- parallel:
    id: gates
    join: all
    branches:
      unit: [{verify: {command: pytest -q}}]
      lint: [{verify: {command: ruff check .}}]
- graph:
    fragment: {entry: start, exits: {pass: done}, effects: {mode: read-only, writes: []}}
    nodes: {}
    edges: []
- escalate: {}
'''
    )

    workflow = parse_workflow(load_workflow(workflow_file))

    assert [type(block).__name__ for block in workflow.flow] == [
        "StepIR", "VerifyIR", "DecideIR", "ChooseIR", "RepeatIR", "CallIR",
        "AcceptIR", "ParallelIR", "GraphIR", "EscalateIR",
    ]
    assert workflow.flow[8].kind == "inline"


@pytest.mark.parametrize("legacy_field", ["writes", "junit"])
def test_verify_rejects_legacy_file_result_and_write_surfaces(
    workflow_file: Path, legacy_field: str
) -> None:
    value = "[reports/]" if legacy_field == "writes" else "{path: reports/junit.xml}"
    workflow_file.write_text(
        BASE
        + "flow:\n"
        + "- verify:\n"
        + "    command: pytest -q\n"
        + f"    {legacy_field}: {value}\n"
    )

    error = raises_diagnostic("LSW1", workflow_file)

    assert error.pointer == f"/flow/0/verify/{legacy_field}"


def test_accept_rejects_legacy_artifact_plus_hash_from_form(
    workflow_file: Path,
) -> None:
    workflow_file.write_text(
        BASE
        + "flow:\n"
        + "- accept:\n"
        + "    artifact: review.md\n"
        + "    hash_from: review.review\n"
        + "    verdict: PASS\n"
    )

    error = raises_diagnostic("LSW1", workflow_file)

    assert error.pointer in {"/flow/0/accept/artifact", "/flow/0/accept/hash_from"}


@pytest.mark.parametrize(
    "block",
    [
        "race: {}",
        "parallel: {id: gates, join: any, branches: {one: [], two: []}}",
        "parallel: {id: gates, join: all, branches: {one: [], two: []}, race: true}",
        "choose: {value: risk, cases: {}, goto: elsewhere}",
    ],
)
def test_v2_only_syntax_is_an_explicit_v1_error(workflow_file: Path, block: str) -> None:
    workflow_file.write_text(BASE + "flow:\n- " + block + "\n")

    assert raises_diagnostic("LSW120", workflow_file).pointer.startswith("/flow/0")
