"""Behavioral coverage for the daily-change-reviewed example recipes."""

from __future__ import annotations

from pathlib import Path

import pytest

from lockstep_mcp.engine import Engine
from _subcall_helpers import write_runners_yaml


EXAMPLES = Path(__file__).resolve().parents[2] / "recipes" / "examples"


def _start_daily_change(tmp_path, monkeypatch):
    state = tmp_path / "state"
    write_runners_yaml(
        state,
        sleep=30.0,
        runner="codex",
        driver="codex",
        model="gpt-5.6-luna",
    )
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "__init__.py").write_text("")
    (project / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left - right\n"
    )
    (project / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (project / "README.md").write_text("Original\n")
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.setenv("LOCKSTEP_RUNNER", "codex")
    engine = Engine(state_dir=state, recipes_dir=EXAMPLES, memory_only=False)

    run = engine.start("daily-change-reviewed", vars={}, project=str(project))
    assert run["step"] == "plan"

    return engine, project, run


def _pass_plan_and_tests(engine, project, run):
    (project / ".lockstep").mkdir()
    (project / ".lockstep" / "plan.md").write_text(
        "# Goal\nFix addition.\n\n# Acceptance Criteria\n2 + 3 is 5.\n\n# Steps\nAdd a test, then fix the code.\n"
    )
    out = engine.done(
        run["run_id"], "plan", {"plan_path": ".lockstep/plan.md"}
    )
    assert out["passed"] is True and out["step"] == "tests"

    (project / "tests").mkdir()
    (project / "tests" / "test_calculator.py").write_text(
        "from src.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    out = engine.done(run["run_id"], "tests", {"summary": "Added regression test"})
    assert out["passed"] is True and out["step"] == "implement"


def _pass_implementation_and_verification(engine, project, run):
    (project / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n"
    )
    out = engine.done(run["run_id"], "implement", {"summary": "Fixed addition"})
    assert out["passed"] is True and out["step"] == "verify"

    out = engine.done(run["run_id"], "verify", {"summary": "Full suite passes"})
    assert out["passed"] is True, out
    assert out["step"] == "_subcall"
    assert out["subcall"]["runner"] == "codex"


def _complete_review(engine, project, run, verdict="PASS"):
    child = engine._runs.children(run["run_id"])[0]
    assert child.recipe == "daily-review-gate"
    (project / ".lockstep" / "review.md").write_text(
        f"# Findings\nReview completed.\n\nVerdict: {verdict}\n"
    )
    child_out = engine.done(
        child.run_id, "review", {"review_path": ".lockstep/review.md"}
    )
    assert child_out["done"] is True

    assert engine.status(run["run_id"])["step"] == "accept"


def test_daily_change_completes_after_tests_implementation_and_codex_review(
    tmp_path, monkeypatch,
):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    _complete_review(engine, project, run)

    out = engine.done(
        run["run_id"], "accept", {"review_path": ".lockstep/review.md"}
    )
    assert out["done"] is True
    assert engine.status(run["run_id"])["status"] == "done"


def test_implementation_cannot_weaken_the_frozen_tests(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    (project / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n"
    )
    (project / "tests" / "test_calculator.py").write_text(
        "from src.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) != 0\n"
    )

    out = engine.done(run["run_id"], "implement", {"summary": "Changed code and test"})

    assert out["passed"] is False
    assert any("tests" in reason for reason in out["reasons"])
    assert engine.status(run["run_id"])["step"] == "implement"


def test_failing_suite_never_launches_the_review(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    (project / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left * right\n"
    )
    out = engine.done(run["run_id"], "implement", {"summary": "Wrong implementation"})
    assert out["passed"] is True and out["step"] == "verify"

    out = engine.done(run["run_id"], "verify", {"summary": "Attempted suite"})

    assert out["passed"] is False
    assert any("junit_gate" in reason for reason in out["reasons"])
    assert engine.status(run["run_id"])["step"] == "verify"
    assert engine._runs.children(run["run_id"]) == []


def test_fail_review_is_rejected_by_the_parent(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    _complete_review(engine, project, run, verdict="FAIL")

    out = engine.done(
        run["run_id"], "accept", {"review_path": ".lockstep/review.md"}
    )

    assert out["passed"] is False
    assert any("review_verdict" in reason for reason in out["reasons"])
    assert engine.status(run["run_id"])["step"] == "accept"


@pytest.mark.parametrize(
    ("relative_path", "replacement"),
    [
        ("src/calculator.py", "def add(left, right):\n    return 999\n"),
        ("tests/test_calculator.py", "def test_nothing():\n    assert True\n"),
        ("pytest.ini", "[pytest]\naddopts = --ignore=tests\n"),
        (".lockstep/plan.md", "# Goal\nChanged after review.\n"),
    ],
)
def test_reviewed_inputs_cannot_change_after_review(
    tmp_path, monkeypatch, relative_path, replacement,
):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    _complete_review(engine, project, run)
    (project / relative_path).write_text(replacement)

    out = engine.done(
        run["run_id"], "accept", {"review_path": ".lockstep/review.md"}
    )

    assert out["passed"] is False
    assert any(relative_path in reason for reason in out["reasons"])
    assert engine.status(run["run_id"])["step"] == "accept"


def test_plan_cannot_change_after_it_passes(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    (project / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n"
    )
    (project / ".lockstep" / "plan.md").write_text(
        "# Goal\nShip anything.\n\n# Acceptance Criteria\nNone.\n\n# Steps\nBypass review.\n"
    )

    out = engine.done(run["run_id"], "implement", {"summary": "Changed code and plan"})

    assert out["passed"] is False
    assert any("plan.md" in reason for reason in out["reasons"])


def test_child_cannot_change_an_unlisted_project_file(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    child = engine._runs.children(run["run_id"])[0]
    (project / ".lockstep" / "review.md").write_text(
        "# Findings\nNone.\n\nVerdict: PASS\n"
    )
    (project / "README.md").write_text("Rewritten by reviewer\n")

    out = engine.done(
        child.run_id, "review", {"review_path": ".lockstep/review.md"}
    )

    assert out["passed"] is False
    assert any("README.md" in reason for reason in out["reasons"])


def test_conflicting_review_verdicts_are_rejected(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    child = engine._runs.children(run["run_id"])[0]
    (project / ".lockstep" / "review.md").write_text(
        "# Findings\nBlocking issue.\n\nVerdict: FAIL\nVerdict: PASS\n"
    )

    out = engine.done(
        child.run_id, "review", {"review_path": ".lockstep/review.md"}
    )

    assert out["passed"] is False
    assert any("review_verdict" in reason for reason in out["reasons"])


def test_accept_cannot_substitute_an_alias_for_the_declared_review(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    _complete_review(engine, project, run)
    original = (project / ".lockstep" / "review.md").read_text()
    (project / ".lockstep" / "review-alias.md").write_text(original)
    (project / ".lockstep" / "review.md").write_text("tampered\n")

    out = engine.done(
        run["run_id"], "accept", {"review_path": ".lockstep/review-alias.md"}
    )

    assert out["accepted"] is False
    assert out["errors"]
