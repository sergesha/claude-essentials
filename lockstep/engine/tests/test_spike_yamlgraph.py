"""Task 1 spike: pin down yamlgraph's actual API/dialect before anything else
in lockstep depends on it. See yamlgraph_api.py module docstring for every
deviation from the plan's assumed dialect, found while writing this spike.
"""

from pathlib import Path

import lockstep_mcp.yamlgraph_api as yg

FIX = Path(__file__).parent / "fixtures" / "recipes" / "good" / "minimal.yaml"


def test_start_parks_on_interrupt():
    app = yg.compile_recipe(FIX, db_path=None)
    adv = yg.start(app, {}, "t1")
    assert adv.done is False
    assert adv.brief is not None
    assert adv.brief.step == "one"
    assert adv.brief.evidence_schema is not None
    assert adv.brief.checks


def test_resume_reaches_end_on_pass():
    app = yg.compile_recipe(FIX, db_path=None)
    yg.start(app, {}, "t2")
    adv = yg.resume(app, {"_verdict_status": "pass"}, "t2")
    assert adv.done is True


def test_sqlite_survives_new_app(tmp_path):
    db = tmp_path / "spike.db"
    app = yg.compile_recipe(FIX, db_path=db)
    yg.start(app, {}, "t3")
    app2 = yg.compile_recipe(FIX, db_path=db)
    adv = yg.resume(app2, {"_verdict_status": "pass"}, "t3")
    assert adv.done is True


def test_cli_validate_ok():
    ok, _msg = yg.cli_validate(FIX)
    assert ok is True


def test_cli_mermaid():
    out = yg.cli_mermaid(FIX, None)
    assert "step_one" in out


def test_fail_retry_then_pass():
    app = yg.compile_recipe(FIX, db_path=None)
    yg.start(app, {}, "t10")
    adv = yg.resume(app, {"_verdict_status": "fail"}, "t10")  # fail verdict -> loops back
    assert adv.done is False and adv.brief.step == "one"  # parked on SAME step again
    adv = yg.resume(app, {"_verdict_status": "pass"}, "t10")
    assert adv.done is True


def test_loop_limit_fires_loop_exit_to_escalate():
    app = yg.compile_recipe(FIX, db_path=None)
    yg.start(app, {}, "t11")
    for _ in range(3):
        adv = yg.resume(app, {"_verdict_status": "fail"}, "t11")
    assert adv.brief is not None and adv.brief.step == "escalate"


def test_loop_counter_survives_restart(tmp_path):
    db = tmp_path / "r.db"
    app = yg.compile_recipe(FIX, db_path=db)
    yg.start(app, {}, "t12")
    yg.resume(app, {"_verdict_status": "fail"}, "t12")
    yg.resume(app, {"_verdict_status": "fail"}, "t12")
    app2 = yg.compile_recipe(FIX, db_path=db)  # fresh process simulation
    adv = yg.resume(app2, {"_verdict_status": "fail"}, "t12")  # 3rd failure
    assert adv.brief.step == "escalate"  # counter lived in the CHECKPOINT


def test_vars_reach_state():
    app = yg.compile_recipe(FIX, db_path=None)
    adv = yg.start(app, {"task_name": "X"}, "t13")
    assert adv.state.get("task_name") == "X"  # vars land in state (brief substitution is engine-side)
