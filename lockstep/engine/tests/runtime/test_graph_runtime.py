from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    proof = restarted.interrupt_lineage(binding.public_run_id, coordinate)
    assert proof is not None
    assert proof.disposition == "descended"
    assert proof.occurrence.coordinate == coordinate
    assert proof.occurrence.value == parked.pending[0].value
    restarted.close()
    store.close()


def test_ensure_started_serializes_two_recoverers_and_never_replays_input(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    state = {"snapshot": NativeSnapshot(values={}), "invocations": 0}

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            assert thread_id == binding.thread_id
            assert subgraphs is True
            return state["snapshot"]

        def invoke(self, values, *, thread_id):
            assert thread_id == binding.thread_id
            state["invocations"] += 1
            state["snapshot"] = NativeSnapshot(
                values=dict(values), checkpoint_id="committed"
            )
            return state["snapshot"]

        def close(self):
            pass

    app = App()

    def runtime():
        candidate = GraphRuntime(
            bundle_store=bundles,
            leases=leases,
            invocations=InvocationLockStore(tmp_path / "owner-state"),
            checkpoint_path=tmp_path / "checkpoints.sqlite",
            app_factory=lambda *_: app,
        )
        candidate.bind(binding)
        return candidate

    first = runtime()
    second = runtime()
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = tuple(
            pool.map(
                lambda item: item[0].ensure_started(binding.public_run_id, item[1]),
                ((first, {"winner": 1}), (second, {"winner": 2})),
            )
        )

    assert state["invocations"] == 1
    assert snapshots[0].checkpoint_id == snapshots[1].checkpoint_id == "committed"
    assert snapshots[0].values == snapshots[1].values
    first.close()
    second.close()
    store.close()


@pytest.mark.parametrize(
    "recipe_name",
    ["sequential_interrupts.recipe.yaml", "parent_then_direct.recipe.yaml"],
)
def test_public_checkpoint_parent_chain_proves_exact_interrupt_ancestry(
    tmp_path, recipe_name
):
    bundles, binding = _binding(tmp_path, FIXTURES / recipe_name)
    store, runtime = _runtime(tmp_path, bundles)
    runtime.bind(binding)
    producer = runtime.start(binding.public_run_id, {}).pending[0]
    consumer_snapshot = runtime.resume(
        binding.public_run_id,
        producer.coordinate,
        {producer.coordinate.interrupt_id: "yes"},
    )
    consumer = consumer_snapshot.pending[0]

    assert runtime.checkpoint_is_ancestor(
        binding.public_run_id, producer.coordinate, consumer
    )

    runtime.close()
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
    assert (
        runtime.snapshot(binding.public_run_id, subgraphs=True).pending
        == parked.pending
    )
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


def test_lineage_rejects_foreign_same_interrupt_id_in_bound_thread(tmp_path):
    from lockstep.runtime.native_models import (
        NativeCoordinate,
        NativeInterrupt,
    )

    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    source = NativeCoordinate(
        binding.thread_id, "source-checkpoint", "child", "source-task", "same-id"
    )
    foreign = replace(
        source, checkpoint_id="foreign-checkpoint", task_id="foreign-task"
    )

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            return NativeSnapshot(values={}, pending=())

        def history(self, *, thread_id):
            return (
                NativeSnapshot(
                    values={},
                    pending=(NativeInterrupt(foreign, {"foreign": True}),),
                ),
            )

        def interrupt_history(self, *, thread_id, checkpoint_ns, snapshot_limit):
            from lockstep.runtime.native_models import NativeInterruptOccurrence

            return (NativeInterruptOccurrence(foreign, {"foreign": True}),)

        def close(self):
            pass

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=LeaseStore(store),
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)

    assert runtime.coordinate_lineage(binding.public_run_id, source) == "incompatible"
    runtime.close()
    store.close()


def test_lineage_rejects_ambiguous_duplicate_exact_occurrences(tmp_path):
    from lockstep.runtime.native_models import (
        NativeCoordinate,
        NativeInterruptOccurrence,
    )

    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    source = NativeCoordinate(
        binding.thread_id, "checkpoint", "child", "task", "interrupt"
    )

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            return NativeSnapshot(values={}, pending=())

        def interrupt_history(self, *, thread_id, checkpoint_ns, snapshot_limit):
            occurrence = NativeInterruptOccurrence(source, {"protected": True})
            return (occurrence, occurrence)

        def close(self):
            pass

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=LeaseStore(store),
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)

    assert runtime.interrupt_lineage(binding.public_run_id, source) is None
    runtime.close()
    store.close()


def test_commitment_guard_serializes_native_commits_and_revalidates_exact_source(
    tmp_path,
):
    from lockstep.runtime.native_models import NativeCoordinate, NativeInterrupt

    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    coordinate = NativeCoordinate(
        binding.thread_id, "checkpoint", "", "task", "interrupt"
    )

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            return NativeSnapshot(
                values={}, pending=(NativeInterrupt(coordinate, {"effect": True}),)
            )

        def close(self):
            pass

    leases = LeaseStore(store)
    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=leases,
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)

    with runtime.commitment_guard(binding.public_run_id, coordinate) as guarded:
        assert guarded.binding == binding
        assert guarded.interrupt.coordinate == coordinate
        with pytest.raises(LeaseUnavailable):
            leases.acquire("invoke", binding.thread_id, "competitor", 60)

    runtime.close()
    store.close()
