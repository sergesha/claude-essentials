"""Behavioral coverage for the daily-change-reviewed example recipes."""

from __future__ import annotations

from pathlib import Path

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
    assert any("Verdict" in reason for reason in out["reasons"])
    assert engine.status(run["run_id"])["step"] == "accept"


def test_source_change_after_review_is_rejected(tmp_path, monkeypatch):
    engine, project, run = _start_daily_change(tmp_path, monkeypatch)
    _pass_plan_and_tests(engine, project, run)
    _pass_implementation_and_verification(engine, project, run)
    _complete_review(engine, project, run)
    (project / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return 999\n"
    )

    out = engine.done(
        run["run_id"], "accept", {"review_path": ".lockstep/review.md"}
    )

    assert out["passed"] is False
    assert any("src" in reason for reason in out["reasons"])
    assert engine.status(run["run_id"])["step"] == "accept"
