"""End-to-end: parent recipe -> fractal review subcall -> hash-pinned verify.

Fake runner only (no tokens). Exercises: spawn, refusals while parked
(done/abort/escalate), restart mid-subcall (engine singleton AND supervisor
handle registry), origin binding live (refusal without the credential, then
success with it), child-terminal poll + hash collection, tamper detection,
nonce redaction.
"""
import hashlib

import pytest

from lockstep_mcp import server, subcalls
from _subcall_helpers import FIX, write_runners_yaml


def test_full_subcall_cycle_with_restart(tmp_path, monkeypatch):
    state = tmp_path / "state"
    write_runners_yaml(state, sleep=30.0)      # runner stays alive while the test plays reviewer (fractal rule 3: running)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("LOCKSTEP_STATE_DIR", str(state))
    monkeypatch.setenv("LOCKSTEP_RECIPES", str(FIX / "good"))
    monkeypatch.setenv("LOCKSTEP_RUNNER", "claude")
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN", raising=False)
    monkeypatch.delenv("LOCKSTEP_CHILD_NONCE", raising=False)
    monkeypatch.chdir(proj)                    # scenario_start captures cwd as the project
    server._reset_engine()

    # 1. start; pass the plan step -> the fractal subcall spawns
    r = server.scenario_start("subcall-fractal")
    (proj / ".lockstep").mkdir()
    (proj / ".lockstep" / "plan.md").write_text("x")
    out = server.scenario_done(r["run_id"], "plan", {"plan_path": ".lockstep/plan.md"})
    assert out["step"] == "_subcall" and out["subcall"]["node"] == "review"

    # 2. while the subcall runs, the parent is untouchable (I10.2a/b)
    refused = server.scenario_done(r["run_id"], "review", {"anything": 1})
    assert refused["accepted"] is False and "subcall in progress" in refused["errors"][0]
    for call in (lambda: server.scenario_abort(r["run_id"]),
                 lambda: server.scenario_escalate(r["run_id"], "impatient")):
        with pytest.raises(Exception) as e:
            call()
        assert "subcall in progress" in str(e.value)
    st = server.scenario_status(r["run_id"])
    assert st["subcall"]["node"] == "review" and st["subcall"]["runner"] == "claude"

    # 3. full restart mid-subcall: engine singleton AND the supervisor
    #    handle registry (I10.4 — _reset_engine alone leaves the Popen
    #    handles; a real restart has neither). Reattach is files-only.
    server._reset_engine()
    subcalls._HANDLES.clear()

    # 4. the test plays the reviewer. WITHOUT the credential: origin
    #    binding refuses (live proof). With it: the child run completes and
    #    its final baseline pins the artifact.
    child = server._eng()._runs.children(r["run_id"])[0]
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\n")
    with pytest.raises(Exception) as e:
        server.scenario_done(child.run_id, "review", {"review_path": ".lockstep/review.md"})
    assert "credential" in str(e.value).lower()
    monkeypatch.setenv("LOCKSTEP_CHILD_RUN", child.run_id)
    monkeypatch.setenv("LOCKSTEP_CHILD_NONCE", child.nonce)
    out = server.scenario_done(child.run_id, "review", {"review_path": ".lockstep/review.md"})
    assert out["done"] is True
    monkeypatch.delenv("LOCKSTEP_CHILD_RUN")
    monkeypatch.delenv("LOCKSTEP_CHILD_NONCE")

    # 5. parent auto-polls: child terminal -> envelope collected (I10.2c)
    st = server.scenario_status(r["run_id"])
    assert st["step"] == "verify"
    env = server._eng()._peek_state(r["run_id"])["_subcall_envelope"]
    digest = hashlib.sha256((proj / ".lockstep" / "review.md").read_bytes()).hexdigest()
    assert env["child_status"] == "done" and env["artifact_hashes"] == {"review": digest}

    # 6. tamper -> verify FAILS on file_matches_hash
    #    (validate_verify loop cap is 2 — this is execution 1 of 2, I10.3)
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\nforged addendum\n")
    out = server.scenario_done(r["run_id"], "verify", {"review_path": ".lockstep/review.md"})
    assert out["passed"] is False and any("hash" in reason for reason in out["reasons"])

    # 7. restore the pinned bytes -> passes (execution 2 of 2); run done
    (proj / ".lockstep" / "review.md").write_text("Verdict: PASS\n")
    out = server.scenario_done(r["run_id"], "verify", {"review_path": ".lockstep/review.md"})
    assert out["done"] is True
    assert server.scenario_status(r["run_id"])["status"] == "done"

    # 8. the credential never went on the wire
    assert all("nonce" not in d for d in server.list_runs())
