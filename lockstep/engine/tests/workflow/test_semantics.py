from __future__ import annotations

from pathlib import Path

import pytest

from lockstep.workflow.diagnostics import DiagnosticError
from lockstep.workflow.ir import (
    AcceptIR, CallIR, ChooseIR, DecideIR, EscalateIR, ParallelIR, RepeatIR, StepIR,
    VerifyIR, WorkflowIR,
)
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import (
    ChildArtifactContract,
    ChildWorkflowContract,
    InMemoryWorkflowCatalog,
    OutcomeProvenance,
    validate_semantics,
)


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


def semantic_error(workflow, catalog=None):
    with pytest.raises(DiagnosticError) as raised:
        validate_semantics(workflow, catalog or InMemoryWorkflowCatalog({}))
    return raised.value.diagnostics[0]


def review_contract(*, non_artifact_writes: tuple[str, ...] = ()) -> ChildWorkflowContract:
    return ChildWorkflowContract(
        outcomes=("pass", "fail", "error"),
        exports={"review": ChildArtifactContract("review", "review.md")},
        non_artifact_writes=non_artifact_writes,
    )


def test_project_command_cannot_become_a_trusted_decision() -> None:
    """Removing the provider allowlist must reject a direct-IR project command."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (DecideIR("risk", {"type": "changed-paths", "since": "start", "cases": {}, "default": "low", "command": "./risk.sh"}),),
    )

    error = semantic_error(workflow)

    assert error.code == "LSW301"
    assert error.pointer == "/flow/0/decide/using"


def test_choose_accepts_only_a_prior_trusted_outcome_and_exhausts_its_enum(
    workflow_file: Path,
) -> None:
    """Dropping enum exhaustiveness would let a trusted result fall through."""
    workflow = parse(
        workflow_file,
        '''\
- decide:
    id: risk
    using: {type: changed-paths, since: start, cases: {high: [auth/**]}, default: low}
- choose:
    value: risk
    cases:
      high: [{escalate: {}}]
''',
    )

    error = semantic_error(workflow)

    assert error.code == "LSW302"
    assert error.pointer == "/flow/1/choose/cases"


def test_choose_rejects_agent_evidence_as_a_routing_symbol(workflow_file: Path) -> None:
    """Changing source lookup to accept arbitrary strings would bypass provenance."""
    workflow = parse(
        workflow_file,
        '''\
- step: plan
  task: Write a plan
  exit: Plan exists
- choose:
    value: evidence.approved
    cases: {yes: [{escalate: {}}]}
    default: [{escalate: {}}]
''',
    )

    error = semantic_error(workflow)

    assert error.code == "LSW301"
    assert error.pointer == "/flow/1/choose/value"


def test_validate_semantics_builds_qualified_call_artifacts_and_effect_contracts(
    workflow_file: Path,
) -> None:
    """A compiler must receive a resolved, immutable parent handle—not raw evidence text."""
    workflow = parse(
        workflow_file,
        '''\
- call:
    id: review
    workflow: independent-review
    runner: codex
    artifacts: {review: .lockstep/review.md}
- accept:
    artifact: .lockstep/review.md
    hash_from: review.review
    verdict: PASS
''',
    )
    catalog = InMemoryWorkflowCatalog({"independent-review": review_contract()})

    validated = validate_semantics(workflow, catalog)

    assert validated.artifacts["review.review"].destination == ".lockstep/review.md"
    assert validated.artifacts["review.review"].source == "review.md"
    assert validated.flow.effects.writes == (".lockstep/review.md",)
    with pytest.raises(TypeError):
        validated.artifacts["other"] = validated.artifacts["review.review"]


def test_call_requires_existing_contract_and_declared_export(workflow_file: Path) -> None:
    """A missing child contract or handle must not be deferred to runtime evidence."""
    workflow = parse(
        workflow_file,
        '''\
- call:
    id: review
    workflow: independent-review
    runner: codex
    artifacts: {missing: .lockstep/review.md}
''',
    )

    error = semantic_error(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))

    assert error.code == "LSW304"
    assert error.pointer == "/flow/0/call/artifacts/missing"


def test_parallel_derives_eligibility_from_child_effect_contract(workflow_file: Path) -> None:
    """A child with undeclared project writes must never become parallel-eligible by a flag."""
    workflow = parse(
        workflow_file,
        '''\
- parallel:
    id: gates
    join: all
    branches:
      security:
        - call:
            id: review
            workflow: independent-review
            runner: codex
            artifacts: {review: review.md}
      lint: [{verify: {command: ruff check .}}]
''',
    )

    error = semantic_error(
        workflow,
        InMemoryWorkflowCatalog({"independent-review": review_contract(non_artifact_writes=("src/",))}),
    )

    assert error.code == "LSP102"
    assert error.pointer == "/flow/0/parallel/branches/security/0/call"


def test_outcome_symbols_record_trusted_provenance(workflow_file: Path) -> None:
    """Decisions must remain distinguishable from agent-controlled evidence."""
    workflow = parse(
        workflow_file,
        '''\
- decide:
    id: risk
    using: {type: changed-paths, since: start, cases: {high: [auth/**]}, default: low}
''',
    )

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({}))

    assert validated.outcomes["risk"].values == ("high", "low")
    assert validated.outcomes["risk"].provenance is OutcomeProvenance.DECISION


def test_parallel_semantics_rejects_direct_ir_accept_block() -> None:
    """Removing the semantic eligibility guard would let a branch write into its parent."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (ParallelIR("gates", "all", {
            "one": (AcceptIR(None, ".lockstep/review.md", "review.review", None, "PASS"),),
            "two": (),
        }),),
    )

    error = semantic_error(workflow)

    assert error.code == "LSP101"
    assert error.pointer == "/flow/0/parallel/branches/one/0"


def test_semantics_rechecks_global_ids_inside_direct_ir_nested_flow() -> None:
    """A direct IR bypass must not allow duplicate node identities through a choose branch."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (VerifyIR("checks", "pytest -q"), ChooseIR(None, "checks", {"pass": (VerifyIR("checks", "ruff check ."),)}, ())),
    )

    error = semantic_error(workflow)

    assert error.code == "LSW110"
    assert error.pointer == "/flow/1/choose/cases/pass/0"


def test_choose_contract_preserves_nested_branches_and_reconverges(workflow_file: Path) -> None:
    """Dropping nested contracts would make a compiler lose branch retry/effect behavior."""
    workflow = parse(
        workflow_file,
        '''\
- decide:
    id: risk
    using: {type: changed-paths, since: start, cases: {high: [auth/**]}, default: low}
- choose:
    value: risk
    cases:
      high:
        - verify: {id: security, command: security-check, retry: {limit: 2, exhausted: escalate}, writes: [.reports/security.txt]}
      low:
        - verify: {id: tests, command: pytest -q}
''',
    )

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({}))
    choose = validated.flow.blocks[1]

    assert choose.reconverges is True
    assert choose.branches["high"].blocks[0].retry.total_executions == 2
    assert choose.effects.writes == (".reports/security.txt",)


@pytest.mark.parametrize(
    ("value", "cases", "default", "expected_pointer"),
    [
        ("unknown", {"yes": ()}, (), "/flow/0/choose/value"),
        ("risk", {"other": ()}, (), "/flow/1/choose/cases/other"),
    ],
)
def test_choose_rejects_unknown_sources_and_labels(
    value: str, cases: dict[str, tuple], default: tuple, expected_pointer: str
) -> None:
    """Permissive routing would turn arbitrary evidence strings into control flow."""
    prefix = () if value == "unknown" else (DecideIR("risk", {"type": "changed-paths", "since": "start", "cases": {}, "default": "low"}),)
    workflow = WorkflowIR("1", "release", "Release safely", ("**",), prefix + (ChooseIR(None, value, cases, default),))

    error = semantic_error(workflow)

    assert error.code in {"LSW301", "LSW302"}
    assert error.pointer == expected_pointer


def test_validator_child_and_parallel_results_have_distinct_trusted_provenance(workflow_file: Path) -> None:
    """Collapsing provenance would make evidence and engine outcomes indistinguishable."""
    workflow = parse(
        workflow_file,
        '''\
- verify: {id: checks, command: pytest -q}
- call: {id: review, workflow: independent-review, runner: codex}
- parallel:
    id: gates
    join: all
    branches:
      one: [{verify: {command: ruff check .}}]
      two: [{verify: {command: pytest -q}}]
''',
    )

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))

    assert validated.outcomes["checks"].provenance is OutcomeProvenance.VALIDATOR
    assert validated.outcomes["review"].provenance is OutcomeProvenance.CHILD
    assert validated.outcomes["gates"].provenance is OutcomeProvenance.PARALLEL


def test_call_without_catalog_contract_is_rejected(workflow_file: Path) -> None:
    """Deferring an unknown child to runtime would make the call DAG unreviewable."""
    workflow = parse(workflow_file, "- call: {id: review, workflow: independent-review, runner: codex}\n")

    error = semantic_error(workflow)

    assert error.code == "LSW304"
    assert error.pointer == "/flow/0/call/workflow"


def test_anonymous_artifact_free_call_is_valid_but_cannot_create_a_routing_symbol(
    workflow_file: Path,
) -> None:
    """Requiring an ID for every call would reject a valid unreferenced child workflow."""
    workflow = parse(workflow_file, "- call: {workflow: independent-review, runner: codex}\n")

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))

    assert "independent-review" not in validated.outcomes
    assert validated.artifacts == {}


def test_choose_cannot_reference_an_anonymous_call(workflow_file: Path) -> None:
    """Naming a workflow is not equivalent to an explicit trusted call-result ID."""
    workflow = parse(
        workflow_file,
        '''\
- call: {workflow: independent-review, runner: codex}
- choose:
    value: independent-review
    cases: {pass: [{escalate: {}}]}
    default: [{escalate: {}}]
''',
    )

    error = semantic_error(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))

    assert error.code == "LSW301"
    assert error.pointer == "/flow/1/choose/value"


@pytest.mark.parametrize(
    ("parallel", "code", "pointer"),
    [
        (ParallelIR("gates", "any", {"one": (), "two": ()}), "LSP101", "/flow/0/parallel/join"),
        (ParallelIR("gates", "all", {"one": ()}), "LSP101", "/flow/0/parallel/branches"),
    ],
)
def test_parallel_rechecks_v1_bounds_when_direct_ir_bypasses_schema(
    parallel: ParallelIR, code: str, pointer: str
) -> None:
    """A direct construction must not re-enable v2 join modes or one-branch parallelism."""
    workflow = WorkflowIR("1", "release", "Release safely", ("**",), (parallel,))

    error = semantic_error(workflow)

    assert error.code == code
    assert error.pointer == pointer


def test_direct_ir_v2_handler_is_rejected_with_stable_diagnostic() -> None:
    """Accepting continue here would silently reintroduce a v2 routing feature."""
    workflow = WorkflowIR("1", "release", "Release safely", ("**",), (VerifyIR("checks", "pytest -q", on_failure="continue"),))

    error = semantic_error(workflow)

    assert error.code == "LSW120"
    assert error.pointer == "/flow/0/verify/on_failure"


def test_parallel_contract_preserves_branch_flow_and_qualified_artifact_handle(workflow_file: Path) -> None:
    """A compiler must retain branch contracts and namespace branch exports before a join."""
    workflow = parse(
        workflow_file,
        '''\
- parallel:
    id: gates
    join: all
    branches:
      security:
        - call:
            id: review
            workflow: independent-review
            runner: codex
            artifacts: {review: review.md}
      lint:
        - verify: {id: lint, command: ruff check ., writes: [.reports/lint.txt]}
''',
    )

    validated = validate_semantics(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))
    parallel = validated.flow.blocks[0]

    assert parallel.branches["lint"].effects.writes == (".reports/lint.txt",)
    assert validated.artifacts["gates.security.review.review"].destination == "review.md"
    assert parallel.effects.writes == ()


@pytest.mark.parametrize(
    "forbidden",
    [
        StepIR(None, "work", "Do work", "Done"),
        ParallelIR("inner", "all", {"one": (), "two": ()}),
    ],
)
def test_parallel_direct_ir_rejects_forbidden_branch_blocks(forbidden) -> None:
    """Schema bypass must not permit agent work or nesting in a parallel branch."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (ParallelIR("gates", "all", {"one": (forbidden,), "two": ()}),),
    )

    error = semantic_error(workflow)

    assert error.code == "LSP101"
    assert error.pointer == "/flow/0/parallel/branches/one/0"


def test_duplicate_call_id_is_rejected_before_ambiguous_handle_resolution() -> None:
    """Changing uniqueness to per-call would allow two producers for review.review."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (
            CallIR("review", "independent-review", "codex", artifacts={"review": ".lockstep/one.md"}),
            CallIR("review", "independent-review", "codex", artifacts={"review": ".lockstep/two.md"}),
        ),
    )

    error = semantic_error(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))

    assert error.code == "LSW110"
    assert error.pointer == "/flow/1"


def test_choose_cannot_reconverge_to_an_artifact_produced_by_only_one_case(
    workflow_file: Path,
) -> None:
    """Leaking a branch-local export past reconvergence would leave accept without bytes on another path."""
    workflow = parse(
        workflow_file,
        '''\
- decide:
    id: risk
    using: {type: changed-paths, since: start, cases: {high: [auth/**]}, default: low}
- choose:
    value: risk
    cases:
      high:
        - call: {id: review, workflow: independent-review, runner: codex, artifacts: {review: .lockstep/review.md}}
      low:
        - verify: {command: pytest -q}
- accept: {artifact: .lockstep/review.md, hash_from: review.review, verdict: PASS}
''',
    )

    error = semantic_error(workflow, InMemoryWorkflowCatalog({"independent-review": review_contract()}))

    assert error.code == "LSW304"
    assert error.pointer == "/flow/2/accept/hash_from"


def test_child_contract_requires_terminal_engine_owned_outcomes(workflow_file: Path) -> None:
    """Treating arbitrary child labels as trusted would let catalog data invent routing values."""
    workflow = parse(workflow_file, "- call: {id: review, workflow: independent-review, runner: codex}\n")
    catalog = InMemoryWorkflowCatalog({
        "independent-review": ChildWorkflowContract(outcomes=("approved",)),
    })

    error = semantic_error(workflow, catalog)

    assert error.code == "LSW304"
    assert error.pointer == "/flow/0/call/workflow"


def test_sequential_call_rejects_child_non_artifact_writes(workflow_file: Path) -> None:
    """A call contract must not grant project writes beyond its declared parent artifacts."""
    workflow = parse(workflow_file, "- call: {id: review, workflow: independent-review, runner: codex}\n")

    error = semantic_error(
        workflow,
        InMemoryWorkflowCatalog({"independent-review": review_contract(non_artifact_writes=("src/",))}),
    )

    assert error.code == "LSW304"
    assert error.pointer == "/flow/0/call"


def test_parallel_direct_ir_rejects_explicit_escalate_block() -> None:
    """Parallel's v1 allowlist is positive; terminal control blocks cannot slip through direct IR."""
    workflow = WorkflowIR(
        "1", "release", "Release safely", ("**",),
        (ParallelIR("gates", "all", {"one": (EscalateIR(),), "two": ()}),),
    )

    error = semantic_error(workflow)

    assert error.code == "LSP101"
    assert error.pointer == "/flow/0/parallel/branches/one/0"


def test_semantic_diagnostic_uses_the_exact_parsed_yaml_mark(workflow_file: Path) -> None:
    """Removing source locations would make semantic diagnostics unactionable in a workflow file."""
    workflow = parse(
        workflow_file,
        '''\
- choose:
    value: evidence.approved
    cases: {yes: [{escalate: {}}]}
    default: [{escalate: {}}]
''',
    )

    error = semantic_error(workflow)

    assert (error.line, error.column, error.pointer) == (7, 12, "/flow/0/choose/value")


def test_direct_ir_semantic_diagnostic_has_no_source_mark() -> None:
    """Direct IR remains valid for compiler tests without inventing a false source location."""
    workflow = WorkflowIR("1", "release", "Release safely", ("**",), (ChooseIR(None, "evidence.approved", {}, ()),))

    error = semantic_error(workflow)

    assert (error.line, error.column) == (None, None)
