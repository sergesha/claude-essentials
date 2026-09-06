"""Protected manual branches keep their own writes and a shared project contract."""

from pathlib import Path

import pytest

from lockstep.workflow.diagnostics import DiagnosticError
from tests.workflow.test_parallel_lowering import _compile, _protected


def _branches(left: str = "src/", right: str = "docs/") -> str:
    return (
        "        code:\n"
        f"          - {{step: edit, task: edit, exit: done, writes: ['{left}']}}\n"
        f"          - {{step: refine, task: refine, exit: done, writes: ['{left}']}}\n"
        "        docs:\n"
        f"          - {{step: document, task: document, exit: done, writes: ['{right}']}}\n"
    )


def test_parallel_manual_steps_keep_exact_writes_and_complete_parallel_contract(
    tmp_path: Path,
) -> None:
    document = _compile(tmp_path, _branches())
    effects = [descriptor for _, _, descriptor in _protected(document)]
    assert [effect.writes for effect in effects] == [("src/",), ("src/",), ("docs/",)]
    assert [effect.parallel.branch for effect in effects] == ["code", "code", "docs"]
    assert all(effect.parallel.id == "gates" for effect in effects)
    assert all(effect.parallel.writes == ("src/", "docs/") for effect in effects)
    assert all(effect.scope_state_keys == () for effect in effects)


@pytest.mark.parametrize(
    "left,right",
    [
        ("src/", "src/file.py"),
        ("src/file.py", "SRC/FILE.py"),
        ("src", "src/file.py"),
        ("src/", "src/"),
        ("Src/left.py", "src/right.py"),
    ],
)
def test_parallel_manual_rejects_cross_branch_write_overlap(
    tmp_path: Path, left: str, right: str
) -> None:
    with pytest.raises(DiagnosticError, match="parallel.*writes.*overlap"):
        _compile(tmp_path, _branches(left, right))


def test_parallel_manual_still_rejects_bounded_scope(tmp_path: Path) -> None:
    with pytest.raises(
        (ValueError, DiagnosticError), match="unmanaged manual.*bounded"
    ):
        _compile(tmp_path, _branches(), timeout=5)


def test_parallel_choose_manual_inherits_all_branch_writes(tmp_path: Path) -> None:
    document = _compile(
        tmp_path,
        "        code:\n"
        "          - verify: {id: check, command: python -m check}\n"
        "          - choose:\n"
        "              value: check\n"
        "              cases:\n"
        "                pass:\n"
        "                  - {step: edit, task: edit, exit: done, writes: ['src/']}\n"
        "              default: []\n"
        "        docs:\n"
        "          - {step: document, task: document, exit: done, writes: ['docs/']}\n",
    )
    effects = [
        effect for _, _, effect in _protected(document) if effect.kind == "manual"
    ]
    assert len(effects) == 2
    assert all(effect.parallel.writes == ("src/", "docs/") for effect in effects)


def test_manual_parallel_contract_does_not_escape_to_following_steps(
    tmp_path: Path,
) -> None:
    import yaml

    from lockstep.workflow.compiler import compile_workflow
    from lockstep.workflow.schema import load_workflow, parse_workflow
    from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics

    _compile(tmp_path, _branches())
    source = tmp_path / "parallel.workflow.yaml"
    source.write_text(
        source.read_text() + "  - {step: after, task: after, exit: done, writes: []}\n"
    )
    catalog = InMemoryWorkflowCatalog({})
    result = compile_workflow(
        validate_semantics(parse_workflow(load_workflow(source)), catalog), catalog
    )
    effects = [
        effect for _, _, effect in _protected(yaml.safe_load(result.recipe_bytes))
    ]
    assert effects[-1].logical_id == "after"
    assert effects[-1].parallel is None


def test_read_only_manual_graph_keeps_working_beside_manual_step(
    tmp_path: Path,
) -> None:
    import textwrap

    import yaml

    from tests.runtime.providers.test_manual import _manual_descriptor

    raw = _manual_descriptor()
    raw["writes"] = []
    graph = {
        "id": "review",
        "fragment": {
            "entry": "ask",
            "exits": {"pass": "passed", "fail": "failed", "error": "errored"},
            "effects": {"mode": "read-only", "writes": []},
        },
        "state": {"request": "dict", "result": "dict"},
        "nodes": {
            "ask": {
                "type": "interrupt",
                "state_key": "request",
                "resume_key": "result",
                "idempotent": False,
                "message": {"lockstep_effect": raw},
            },
            **{
                name: {"type": "passthrough"}
                for name in ("passed", "failed", "errored")
            },
        },
        "edges": [
            {"from": "ask", "to": target, "condition": f"result.outcome == '{outcome}'"}
            for target, outcome in (
                ("passed", "PASS"),
                ("failed", "FAIL"),
                ("errored", "ERROR"),
            )
        ],
    }
    branches = "        review:\n" + textwrap.indent(
        yaml.safe_dump([{"graph": graph}]), "          "
    )
    branches += "        code:\n          - {step: edit-code, task: edit, exit: done, writes: ['src/']}\n"
    effects = [effect for _, _, effect in _protected(_compile(tmp_path, branches))]
    assert len(effects) == 2
    assert all(effect.parallel.writes == ("src/",) for effect in effects)


def test_parallel_manual_artifact_is_in_integrity_surface_but_not_non_artifact_writes(
    tmp_path: Path,
) -> None:
    from lockstep.workflow.schema import load_workflow, parse_workflow
    from lockstep.workflow.semantics import InMemoryWorkflowCatalog, validate_semantics

    document = _compile(
        tmp_path,
        "        report:\n"
        "          - step: report\n"
        "            task: report\n"
        "            exit: done\n"
        "            writes: [report.md]\n"
        "            artifact: {handle: report, path: report.md, markdown: {sections: [Findings]}}\n"
        "        review:\n"
        "          - {step: review, task: review, exit: done, writes: []}\n",
    )
    effects = [effect for _, _, effect in _protected(document)]
    assert all(effect.parallel.writes == ("report.md",) for effect in effects)
    validated = validate_semantics(
        parse_workflow(load_workflow(tmp_path / "parallel.workflow.yaml")),
        InMemoryWorkflowCatalog({}),
    )
    assert validated.non_artifact_writes == ()


def test_managed_child_specialization_drops_shared_manual_contract(
    tmp_path: Path,
) -> None:
    import yaml

    from lockstep.workflow.compiler import compile_workflow
    from lockstep.workflow.schema import load_workflow, parse_workflow
    from lockstep.workflow.semantics import validate_semantics
    from tests.workflow.test_parallel_lowering import _resolved_child

    catalog, _ = _resolved_child(
        tmp_path,
        "  - parallel:\n      id: child-work\n      join: all\n      branches:\n"
        "        left:\n          - {step: left, task: left, exit: done, writes: []}\n"
        "        right:\n          - {step: right, task: right, exit: done, writes: []}\n",
    )
    source = tmp_path / "parent.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: parent\ndescription: parent\nprotect: ['**']\nflow:\n"
        "  - call: {id: child-run, workflow: child, runner: codex}\n"
    )
    compiled = compile_workflow(
        validate_semantics(parse_workflow(load_workflow(source)), catalog), catalog
    )
    effects = []
    for generated in compiled.generated_files:
        document = yaml.safe_load(generated.content)
        if isinstance(document, dict) and "nodes" in document:
            effects.extend(
                effect
                for _, _, effect in _protected(document)
                if effect.kind == "managed"
            )
    assert len(effects) == 2
    assert all(effect.parallel is None for effect in effects)
