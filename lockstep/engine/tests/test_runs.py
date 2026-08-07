"""Task 4: runs.py — RunRecord + RunIndex (runs.json persistence).

RunIndex persists the FULL brief of the parked step (review C4) so
`scenario_status` never needs to peek into the graph checkpoint —
restart-safe by construction (decision 3). Writes are atomic (tmp +
os.replace); timestamps are ISO-8601 UTC; run_id = `<recipe>-<8 hex
uuid4>`; active_only means status == "awaiting".
"""

import re

from lockstep_mcp.runs import RunIndex, TERMINAL_STATUSES

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


def test_terminal_cas_refuses_resurrection(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    idx.update(r.run_id, status="aborted")
    idx.update(r.run_id, status="awaiting", step="one")   # cascade race simulation
    assert idx.get(r.run_id).status == "aborted"


def test_terminal_set_includes_done(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    idx.update(r.run_id, status="done")
    idx.update(r.run_id, status="aborted")               # parent-done cascade must not rewrite
    assert idx.get(r.run_id).status == "done"
    assert TERMINAL_STATUSES == {"done", "escalated", "aborted"}


def test_non_status_fields_still_update_on_terminal(tmp_path):
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    idx.update(r.run_id, status="done")
    idx.update(r.run_id, brief={"step": "x"})
    assert idx.get(r.run_id).brief == {"step": "x"}


def test_parent_and_nonce_roundtrip(tmp_path):
    idx = RunIndex(tmp_path)
    parent = idx.create("parent", "/proj")
    child = idx.create("child", "/proj", parent_run=parent.run_id, nonce="abc123")
    fresh = RunIndex(tmp_path).get(child.run_id)
    assert fresh.parent_run == parent.run_id and fresh.nonce == "abc123"
    assert RunIndex(tmp_path).get(parent.run_id).parent_run is None


def test_descendants_are_recursive(tmp_path):
    idx = RunIndex(tmp_path)
    a = idx.create("a", "/p"); b = idx.create("b", "/p", parent_run=a.run_id)
    c = idx.create("c", "/p", parent_run=b.run_id)
    ids = {r.run_id for r in idx.descendants(a.run_id)}
    assert ids == {b.run_id, c.run_id}
    assert [r.run_id for r in idx.children(a.run_id)] == [b.run_id]


def test_writes_are_serialized_under_lock(tmp_path):
    import threading
    idx = RunIndex(tmp_path)
    runs = [idx.create(f"r{i}", "/p") for i in range(4)]
    def bump(rec):
        for i in range(25):
            RunIndex(tmp_path).update(rec.run_id, step=f"s{i}")
    ts = [threading.Thread(target=bump, args=(r,)) for r in runs]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(RunIndex(tmp_path).list()) == 4          # no record lost to a racing write
    assert all(RunIndex(tmp_path).get(r.run_id).step == "s24" for r in runs)
