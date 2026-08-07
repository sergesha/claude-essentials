"""Task 4: runs.py — RunRecord + RunIndex (runs.json persistence).

RunIndex persists the FULL brief of the parked step (review C4) so
`scenario_status` never needs to peek into the graph checkpoint —
restart-safe by construction (decision 3). Writes are atomic (tmp +
os.replace); timestamps are ISO-8601 UTC; run_id = `<recipe>-<8 hex
uuid4>`; active_only means status == "awaiting".
"""

import json
import re
from pathlib import Path

import pytest

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


def test_status_none_cannot_bypass_terminal_cas(tmp_path):
    # C1: `status=None` must not slip past the CAS (which keys on presence,
    # not truthiness) nor write `status: null` — the springboard for a later
    # resurrection.
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    idx.update(r.run_id, status="done")
    with pytest.raises(ValueError):
        idx.update(r.run_id, status=None)
    assert idx.get(r.run_id).status == "done"
    idx.update(r.run_id, status="awaiting")              # CAS strips it silently
    assert idx.get(r.run_id).status == "done"


def test_bogus_status_string_is_rejected(tmp_path):
    # C1: any status outside {"awaiting"} | TERMINAL_STATUSES is refused —
    # terminal or not — so no unknown value can ever sit in the index.
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    with pytest.raises(ValueError):
        idx.update(r.run_id, status="zombie")
    assert idx.get(r.run_id).status == "awaiting"
    idx.update(r.run_id, status="done")
    with pytest.raises(ValueError):
        idx.update(r.run_id, status="resurrected")
    assert idx.get(r.run_id).status == "done"


def test_update_rejects_unknown_fields_before_save(tmp_path):
    # I1: an unknown field must raise BEFORE _save — a persisted bogus key
    # would make every later get()/list() raise TypeError (index poisoned,
    # Stop hook fails open).
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    with pytest.raises(ValueError):
        idx.update(r.run_id, bogus="x")
    assert idx.get(r.run_id).status == "awaiting"        # index still loads
    assert len(RunIndex(tmp_path).list()) == 1


def test_update_refuses_identity_and_credential_fields(tmp_path):
    # I1: run_id/started are identity, parent_run/nonce are the anti-forgery
    # credential — all set only at create(), never via update().
    idx = RunIndex(tmp_path)
    r = idx.create("rec", "/proj")
    # `run_id` can't even be spelled as a field kwarg (collides with the
    # positional parameter → TypeError at the call layer); the other three
    # hit the mutable-field validation.
    with pytest.raises(TypeError):
        idx.update(r.run_id, **{"run_id": "forged"})
    for field in ("started", "parent_run", "nonce"):
        with pytest.raises(ValueError):
            idx.update(r.run_id, **{field: "forged"})
    got = idx.get(r.run_id)
    assert got.parent_run is None and got.nonce is None and got.started == r.started


def test_create_raises_on_run_id_collision(tmp_path, monkeypatch):
    import lockstep_mcp.runs as runs_mod

    class _FixedUUID:
        hex = "deadbeefcafef00d"

    monkeypatch.setattr(runs_mod.uuid, "uuid4", lambda: _FixedUUID)
    idx = RunIndex(tmp_path)
    idx.create("rec", "/p")
    with pytest.raises(RuntimeError):
        idx.create("rec", "/p")


def test_descendants_survive_a_forged_parent_cycle(tmp_path):
    # update() refuses parent_run rewrites, so forge the cycle on disk —
    # descendants must terminate, not loop forever.
    idx = RunIndex(tmp_path)
    a = idx.create("a", "/p")
    b = idx.create("b", "/p", parent_run=a.run_id)
    data = json.loads((tmp_path / "runs.json").read_text())
    data[a.run_id]["parent_run"] = b.run_id
    (tmp_path / "runs.json").write_text(json.dumps(data))
    assert {r.run_id for r in idx.descendants(a.run_id)} == {b.run_id}


def test_descendants_take_one_snapshot(tmp_path, monkeypatch):
    # One list() snapshot walked in memory — per-frontier re-listing can
    # splice two different index states into one answer.
    idx = RunIndex(tmp_path)
    a = idx.create("a", "/p")
    b = idx.create("b", "/p", parent_run=a.run_id)
    idx.create("c", "/p", parent_run=b.run_id)
    calls = []
    real_list = RunIndex.list

    def counting(self, *args, **kwargs):
        calls.append(1)
        return real_list(self, *args, **kwargs)

    monkeypatch.setattr(RunIndex, "list", counting)
    assert len(idx.descendants(a.run_id)) == 2
    assert len(calls) == 1


def _mp_worker(state_dir: str, wid: int, n_updates: int, barrier) -> None:
    # Module-level: spawn-pickled into a GENUINELY separate process, so the
    # lock under test is the on-disk sidecar, not any process-local state.
    idx = RunIndex(Path(state_dir))
    barrier.wait()
    rec = idx.create(f"w{wid}", "/p")
    for i in range(n_updates):
        idx.update(rec.run_id, step=f"s{i}")


def test_writes_are_serialized_under_lock(tmp_path):
    # I8: separate PROCESSES, barrier-released together, each create+update
    # racing the shared runs.json — a process-local or null lock loses
    # records/updates here.
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    n_workers, n_updates = 4, 10
    barrier = ctx.Barrier(n_workers)
    procs = [
        ctx.Process(target=_mp_worker, args=(str(tmp_path), w, n_updates, barrier))
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert all(p.exitcode == 0 for p in procs)
    records = RunIndex(tmp_path).list()
    assert len(records) == n_workers                     # no create lost to a racing writer
    assert {r.recipe for r in records} == {f"w{w}" for w in range(n_workers)}
    assert all(r.step == f"s{n_updates - 1}" for r in records)


def test_every_save_happens_inside_a_lock_hold(tmp_path, monkeypatch):
    # I8 (deterministic half): every _save — create's included — must occur
    # between file_lock enter and exit.
    import lockstep_mcp.runs as runs_mod
    from contextlib import contextmanager

    real_lock = runs_mod.file_lock
    counters = {"held": 0, "saves": 0, "saves_in_hold": 0}

    @contextmanager
    def recording_lock(target, **kwargs):
        with real_lock(target, **kwargs):
            counters["held"] += 1
            try:
                yield
            finally:
                counters["held"] -= 1

    real_save = runs_mod.RunIndex._save

    def spying_save(self, data):
        counters["saves"] += 1
        if counters["held"] == 1:
            counters["saves_in_hold"] += 1
        return real_save(self, data)

    monkeypatch.setattr(runs_mod, "file_lock", recording_lock)
    monkeypatch.setattr(runs_mod.RunIndex, "_save", spying_save)
    idx = runs_mod.RunIndex(tmp_path)
    r = idx.create("rec", "/p")
    idx.update(r.run_id, step="x")
    assert counters["saves"] == 2
    assert counters["saves_in_hold"] == 2
