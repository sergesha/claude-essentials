from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.graph_runtime import (
    MAX_HISTORY_SNAPSHOTS,
    GraphRuntime,
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore, LeaseUnavailable
from lockstep.runtime.native_models import NativeEvent, NativeSnapshot
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.storage import SQLiteStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "native"


def _binding(tmp_path: Path, recipe: Path, run_id: str = "run-1"):
    bundle_store = RecipeBundleStore(tmp_path / "owner")
    admitted = (
        StrictRecipeIngress(recipe.parent)
        .inspect(recipe.name)
        .authorize(RecipeAuthorityPolicy())
        .capture(bundle_store)
    )
    binding = RunBinding(
        public_run_id=run_id,
        thread_id=f"thread-{run_id}",
        recipe_digest=admitted.definition_sha256,
        recipe_snapshot_ref=admitted.bundle.digest,
        project_identity=str(tmp_path / "project"),
    )
    return bundle_store, binding


def _runtime(tmp_path: Path, bundle_store: RecipeBundleStore):
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store, clock=lambda: datetime.now(UTC))
    runtime = GraphRuntime(
        bundle_store=bundle_store,
        leases=leases,
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=yg.open_native_app,
    )
    return store, runtime


def test_fresh_start_restart_history_and_live_source_deletion(tmp_path):
    source = tmp_path / "recipes" / "parent.recipe.yaml"
    source.parent.mkdir()
    source.write_bytes((FIXTURES / "parent_direct.recipe.yaml").read_bytes())
    (source.parent / "child_interrupt.recipe.yaml").write_bytes(
        (FIXTURES / "child_interrupt.recipe.yaml").read_bytes()
    )
    bundles, binding = _binding(tmp_path, source)
    store, first = _runtime(tmp_path, bundles)
    first.bind(binding)
    parked = first.start(binding.public_run_id, {})
    source.unlink()
    (source.parent / "child_interrupt.recipe.yaml").unlink()
    coordinate = parked.pending[0].coordinate
    assert tuple(first.history(binding.public_run_id))
    first.close()
    store.close()

    store, restarted = _runtime(tmp_path, bundles)
    restarted.bind(binding)
    completed = restarted.resume(
        binding.public_run_id,
        coordinate,
        {coordinate.interrupt_id: "yes"},
    )
    assert completed.values["answer"] == "yes"
    assert completed.pending == ()
    restarted.close()
    store.close()


def test_resume_rejects_stale_checkpoint_wrong_task_and_unknown_interrupt(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store, runtime = _runtime(tmp_path, bundles)
    runtime.bind(binding)
    parked = runtime.start(binding.public_run_id, {})
    current = parked.pending[0].coordinate

    for bad in (
        replace(current, checkpoint_id="stale"),
        replace(current, task_id="wrong"),
        replace(current, interrupt_id="wrong"),
    ):
        with pytest.raises(NativeCoordinateRejected):
            runtime.resume(binding.public_run_id, bad, {bad.interrupt_id: "x"})
    assert runtime.snapshot(binding.public_run_id, subgraphs=True).pending == parked.pending
    runtime.close()
    store.close()


def test_batch_resume_preserves_native_parallel_join(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parallel_interrupts.recipe.yaml")
    store, runtime = _runtime(tmp_path, bundles)
    runtime.bind(binding)
    parked = runtime.start(binding.public_run_id, {})
    assert len(parked.pending) == 2
    answers = {"Branch A?": "alpha", "Branch B?": "beta"}
    results = {
        interrupt.coordinate.interrupt_id: answers[interrupt.value]
        for interrupt in parked.pending
    }
    completed = runtime.resume(
        binding.public_run_id,
        parked.pending[0].coordinate,
        results,
    )
    assert completed.pending == ()
    assert completed.values["answer_a"] == "alpha"
    assert completed.values["answer_b"] == "beta"
    assert completed.values["joined"] is True
    runtime.close()
    store.close()


def test_runtime_closes_adapter_on_normal_teardown(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    closed: list[str] = []

    class App:
        def close(self):
            closed.append("closed")

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=leases,
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)
    runtime.close()
    assert closed == ["closed"]
    store.close()


def test_runtime_closes_every_adapter_when_one_close_fails(tmp_path):
    bundles, first = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    second = replace(first, public_run_id="run-2", thread_id="thread-run-2")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    closed: list[str] = []

    class App:
        def __init__(self, name: str, fail: bool) -> None:
            self.name = name
            self.fail = fail

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("close failed")

    apps = iter((App("first", True), App("second", False)))
    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=leases,
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: next(apps),
    )
    runtime.bind(first)
    runtime.bind(second)
    with pytest.raises(RuntimeError, match="close failed"):
        runtime.close()
    assert closed == ["first", "second"]
    store.close()


def test_stream_holds_the_invocation_lease_until_iteration_finishes(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)

    class App:
        def stream(self, input_or_command, *, thread_id):
            yield NativeEvent(mode="values", data={"input": input_or_command})

        def close(self):
            pass

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=leases,
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)
    events = iter(runtime.stream(binding.public_run_id, {"work": True}))
    assert next(events).data == {"input": {"work": True}}
    with pytest.raises(LeaseUnavailable):
        leases.acquire("invoke", binding.thread_id, "competitor", 60)
    with pytest.raises(StopIteration):
        next(events)
    lease = leases.acquire("invoke", binding.thread_id, "competitor", 60)
    leases.release(lease)
    runtime.close()
    store.close()


def test_public_history_consumption_has_a_hard_ceiling(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    consumed = []

    class App:
        def history(self, *, thread_id):
            for index in range(MAX_HISTORY_SNAPSHOTS + 10_000):
                consumed.append(index)
                yield NativeSnapshot(values={"index": index})

        def close(self):
            pass

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=leases,
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)
    with pytest.raises(NativeHistoryLimitExceeded):
        tuple(runtime.history(binding.public_run_id))
    assert len(consumed) == MAX_HISTORY_SNAPSHOTS + 1
    runtime.close()
    store.close()
