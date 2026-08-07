"""Task 4: runs.py — RunRecord + RunIndex (runs.json persistence).

RunIndex persists the FULL brief of the parked step (review C4) so
`scenario_status` never needs to peek into the graph checkpoint —
restart-safe by construction (decision 3). Writes are atomic (tmp +
os.replace); timestamps are ISO-8601 UTC; run_id = `<recipe>-<8 hex
uuid4>`; active_only means status == "awaiting".
"""

import re

from lockstep_mcp.runs import RunIndex

ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def test_create_sets_run_id_shape_and_defaults(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("feature-dev", "/proj")
    assert re.fullmatch(r"feature-dev-[0-9a-f]{8}", r.run_id)
    assert r.recipe == "feature-dev"
    assert r.project == "/proj"
    assert r.status == "awaiting"
    assert r.step is None
    assert r.brief is None
    assert ISO_PREFIX_RE.match(r.started)
    assert ISO_PREFIX_RE.match(r.updated)


def test_get_missing_raises_keyerror(tmp_path):
    idx = RunIndex(tmp_path)
    try:
        idx.get("nope")
    except KeyError:
        return
    assert False, "expected KeyError"


def test_lifecycle_create_update_get(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("feature-dev", "/proj")
    updated = idx.update(r.run_id, step="plan", status="awaiting")
    assert updated.step == "plan"
    assert updated.updated >= r.updated
    got = idx.get(r.run_id)
    assert got.step == "plan"
    assert got.run_id == r.run_id


def test_list_filters_by_project(tmp_path):
    idx = RunIndex(tmp_path)
    idx.create("recipe-a", "/proj-a")
    idx.create("recipe-b", "/proj-b")
    assert len(idx.list(project="/proj-a")) == 1
    assert len(idx.list()) == 2


def test_list_active_only_excludes_done(tmp_path):
    idx = RunIndex(tmp_path)
    r1 = idx.create("recipe-a", "/proj")
    r2 = idx.create("recipe-b", "/proj")
    idx.update(r2.run_id, status="done")
    active = idx.list(project="/proj", active_only=True)
    assert [r.run_id for r in active] == [r1.run_id]


def test_active_only_excludes_every_terminal_status(tmp_path):
    idx = RunIndex(tmp_path)
    for status in ("done", "escalated", "aborted"):
        r = idx.create(f"recipe-{status}", "/proj")
        idx.update(r.run_id, status=status)
    assert idx.list(project="/proj", active_only=True) == []


def test_index_survives_reopen(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("feature-dev", "/proj")
    idx.update(r.run_id, step="implement")
    reopened = RunIndex(tmp_path)
    got = reopened.get(r.run_id)
    assert got.step == "implement"


def test_db_path_shape(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("feature-dev", "/proj")
    assert idx.db_path(r.run_id) == tmp_path / "runs" / f"{r.run_id}.db"


def test_brief_round_trip_through_fresh_index(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("feature-dev", "/proj")
    brief = {"step": "plan", "task": "t", "exit_criterion": "x", "checks": []}
    idx.update(r.run_id, brief=brief)
    fresh = RunIndex(tmp_path)
    got = fresh.get(r.run_id)
    assert got.brief == brief
