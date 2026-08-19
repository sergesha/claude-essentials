from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.workflow.diagnostics import DiagnosticError
from lockstep.workflow.ir import RepeatIR, VerifyIR, WorkflowIR
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics


BASE = '''\
workflow_version: "1"
name: release
description: Release safely
protect: ["**"]
'''


@pytest.fixture
def workflow_file(tmp_path: Path) -> Path:
    return tmp_path / "release.workflow.yaml"


def parse(workflow_file: Path, flow: str):
    workflow_file.write_text(BASE + "flow:\n" + flow)
    return parse_workflow(load_workflow(workflow_file))


def semantic_error(workflow):
    with pytest.raises(DiagnosticError) as raised:
        validate_semantics(workflow, InMemoryWorkflowCatalog({}))
    return raised.value.diagnostics[0]


def test_repeat_final_fail_exhausts_without_an_extra_iteration(workflow_file: Path) -> None:
    """An off-by-one repeat counter would execute a fourth terminal validator."""
    workflow = parse(
        workflow_file,
        '''\
- repeat:
    id: implementation-cycle
    limit: 3
    until: tests.passed
    exhausted: escalate
    do:
      - step: implement
        task: Implement the change
        exit: Change exists
      - verify: {id: tests, command: pytest -q}
''',
    )

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({}))
    model = validated.flow.blocks[0].simulate(("fail", "fail", "fail"))

    assert model.iterations == 3
    assert model.outcome == "escalate"


def test_repeat_pass_exits_on_the_referenced_terminal_producer(workflow_file: Path) -> None:
    """Ignoring a PASS would continue a completed loop and repeat side effects."""
    workflow = parse(
        workflow_file,
        '''\
- repeat:
    id: implementation-cycle
    limit: 3
    until: tests.passed
    exhausted: escalate
    do:
      - verify: {id: tests, command: pytest -q}
''',
    )

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({}))

    assert validated.flow.blocks[0].simulate(("fail", "pass")).iterations == 2
    assert validated.flow.blocks[0].simulate(("fail", "pass")).outcome == "pass"


@pytest.mark.parametrize(
    ("body", "until", "pointer"),
    [
        ("- verify: {id: tests, command: pytest -q}\n      - verify: {id: lint, command: ruff check .}", "tests.passed", "/flow/0/repeat/until"),
        ("- choose:\n          value: missing\n          cases: {yes: [{verify: {id: tests, command: pytest -q}}]}", "tests.passed", "/flow/0/repeat/until"),
    ],
)
def test_repeat_requires_one_last_terminal_producer_on_every_path(
    workflow_file: Path, body: str, until: str, pointer: str
) -> None:
    """Allowing skipped or non-terminal producers would make repeat routing ambiguous."""
    workflow = parse(
        workflow_file,
        f'''\
- repeat:
    id: implementation-cycle
    limit: 2
    until: {until}
    exhausted: escalate
    do:
      {body}
''',
    )

    error = semantic_error(workflow)

    assert error.code == "LSW303"
    assert error.pointer == pointer


def test_retry_limit_means_total_validator_executions(workflow_file: Path) -> None:
    """Treating retry.limit as retries would silently permit one excess execution."""
    workflow = parse(workflow_file, "- verify: {id: tests, command: pytest -q, retry: {limit: 2, exhausted: escalate}}\n")

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({}))

    assert validated.flow.blocks[0].retry.limit == 2
    assert validated.flow.blocks[0].retry.total_executions == 2


def test_default_retry_is_applied_as_a_total_execution_budget(workflow_file: Path) -> None:
    """Dropping defaults.retry would make an otherwise bounded validator unbounded."""
    workflow_file.write_text(
        BASE
        + '''\
defaults: {retry: {limit: 3, exhausted: escalate}}
flow:
- verify: {id: tests, command: pytest -q}
'''
    )
    workflow = parse_workflow(load_workflow(workflow_file))

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({}))

    assert validated.flow.blocks[0].retry.total_executions == 3


def test_repeat_error_escalates_immediately_without_another_iteration(workflow_file: Path) -> None:
    """Treating ERROR as retryable would repeat after an integrity or execution fault."""
    workflow = parse(
        workflow_file,
        '''\
- repeat:
    id: cycle
    limit: 3
    until: tests.passed
    exhausted: escalate
    do: [{verify: {id: tests, command: pytest -q}}]
''',
    )

    result = validate_semantics(workflow, InMemoryWorkflowCatalog({})).flow.blocks[0].simulate(("error",))

    assert (result.iterations, result.outcome) == (1, "escalate")


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        ((VerifyIR("tests", "pytest -q"), VerifyIR("tests", "ruff check .")), "/flow/0/repeat/until"),
        ((VerifyIR("tests", "pytest -q"), VerifyIR("lint", "ruff check .")), "/flow/0/repeat/until"),
    ],
)
def test_repeat_direct_ir_rejects_double_or_nonfinal_terminal_producer(body, pointer: str) -> None:
    """A repeated or non-final producer makes the loop's normal exit ambiguous."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (RepeatIR("cycle", 2, "tests.passed", body, "escalate"),),
    )

    error = semantic_error(workflow)

    assert error.code in {"LSW110", "LSW303"}
    assert error.pointer == pointer


@pytest.mark.parametrize(
    ("defaults", "terminal", "pointer"),
    [
        ("", "{id: tests, command: pytest -q, retry: {limit: 2, exhausted: escalate}}", "/flow/0/repeat/do/0/verify/retry"),
        ("defaults: {retry: {limit: 2, exhausted: escalate}}\n", "{id: tests, command: pytest -q}", "/flow/0/repeat/do/0/verify/retry"),
    ],
)
def test_repeat_terminal_producer_cannot_retry(
    workflow_file: Path, defaults: str, terminal: str, pointer: str
) -> None:
    """A retried terminal producer could execute more than once per repeat iteration."""
    workflow_file.write_text(
        BASE
        + defaults
        + f'''\
flow:
- repeat:
    id: cycle
    limit: 2
    until: tests.passed
    exhausted: escalate
    do: [{{verify: {terminal}}}]
'''
    )
    workflow = parse_workflow(load_workflow(workflow_file))

    error = semantic_error(workflow)

    assert error.code == "LSW303"
    assert error.pointer == pointer


def test_repeat_rejects_a_path_that_escalates_before_its_terminal_producer(workflow_file: Path) -> None:
    """A path that exits before tests runs violates the repeat's exact-once contract."""
    workflow = parse(
        workflow_file,
        '''\
- repeat:
    id: cycle
    limit: 2
    until: tests.passed
    exhausted: escalate
    do:
      - escalate: {}
      - verify: {id: tests, command: pytest -q}
''',
    )

    error = semantic_error(workflow)

    assert error.code == "LSW303"
    assert error.pointer == "/flow/0/repeat/do/0"


def test_repeat_rejects_choose_path_that_skips_terminal_producer(workflow_file: Path) -> None:
    """A nested branch may not bypass the final producer before flow reconverges."""
    workflow = parse(
        workflow_file,
        '''\
- decide:
    id: risk
    using: {type: changed-paths, since: start, cases: {high: [auth/**]}, default: low}
- repeat:
    id: cycle
    limit: 2
    until: tests.passed
    exhausted: escalate
    do:
      - choose:
          value: risk
          cases:
            high: [{escalate: {}}]
            low: [{verify: {command: ruff check .}}]
      - verify: {id: tests, command: pytest -q}
''',
    )

    error = semantic_error(workflow)

    assert error.code == "LSW303"
    assert error.pointer == "/flow/1/repeat/do/0/choose/cases/high/0"


def test_repeat_exposes_exact_one_terminal_producer_per_normal_path(workflow_file: Path) -> None:
    """Compiler lowering needs an explicit cardinality contract, not an inferred convention."""
    workflow = parse(
        workflow_file,
        '''\
- repeat:
    id: cycle
    limit: 2
    until: tests.passed
    exhausted: escalate
    do: [{verify: {id: tests, command: pytest -q}}]
''',
    )

    repeat = validate_semantics(workflow, InMemoryWorkflowCatalog({})).flow.blocks[0]

    assert repeat.control.terminal_producer == "tests"
    assert repeat.control.producer_cardinalities == (1,)
    assert repeat.control.falls_through is True


def test_repeat_case_named_default_is_not_the_default_branch(workflow_file: Path) -> None:
    """Treating the literal label as the default branch corrupts its source pointer."""
    workflow = parse(
        workflow_file,
        '''\
- decide:
    id: risk
    using: {type: changed-paths, since: start, cases: {}, default: default}
- repeat:
    id: cycle
    limit: 2
    until: tests.passed
    exhausted: escalate
    do:
      - choose:
          value: risk
          cases:
            default: [{escalate: {}}]
      - verify: {id: tests, command: pytest -q}
''',
    )

    error = semantic_error(workflow)

    assert error.code == "LSW303"
    assert error.pointer == "/flow/1/repeat/do/0/choose/cases/default/0"
