from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lockstep.recipe import yamlgraph_adapter as yg
from lockstep.recipe.authority import RecipeAuthorityPolicy, StrictRecipeIngress
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.engine_drive_service import EngineDriveService
from lockstep.runtime.graph_runtime import (
    MAX_HISTORY_SNAPSHOTS,
    GraphRuntime,
    NativeCoordinateRejected,
    NativeHistoryLimitExceeded,
)
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore, LeaseUnavailable
from lockstep.runtime.native_models import NativeAppPort, NativeEvent, NativeSnapshot
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


@pytest.mark.parametrize(
    "method",
    (NativeAppPort.checkpoint_is_ancestor, yg.NativeApp.checkpoint_is_ancestor),
)
def test_native_ancestry_port_names_both_exact_checkpoint_pairs(method):
    parameters = tuple(inspect.signature(method).parameters.values())
    assert tuple(item.name for item in parameters) == (
        "self",
        "thread_id",
        "ancestor_checkpoint_ns",
        "ancestor_checkpoint_id",
        "descendant_checkpoint_ns",
        "descendant_checkpoint_id",
        "snapshot_limit",
    )
    assert tuple(item.kind for item in parameters[1:]) == (
        inspect.Parameter.KEYWORD_ONLY,
    ) * 6
    assert tuple(item.default for item in parameters[1:]) == (
        inspect.Parameter.empty,
    ) * 6


def test_bind_reports_new_lifecycle_ownership_atomically(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store, runtime = _runtime(tmp_path, bundles)
    try:
        assert runtime.bind(binding) is True
        assert runtime.bind(binding) is False
    finally:
        runtime.close()
        store.close()


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


def test_snapshot_serializes_with_unbind_lifecycle_guard(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    invocations = InvocationLockStore(tmp_path / "owner-state")
    snapshot_entered = threading.Event()

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            snapshot_entered.set()
            return NativeSnapshot(values={})

        def close(self):
            pass

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=leases,
        invocations=invocations,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    runtime.bind(binding)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with invocations.hold(binding.thread_id):
            pending = pool.submit(runtime.snapshot, binding.public_run_id)
            assert not snapshot_entered.wait(0.1)
        assert pending.result(timeout=1) == NativeSnapshot(values={})
    runtime.close()
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


def test_decision_guard_serializes_snapshot_to_decision_across_runtimes(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    state = {"version": 0}
    first_entered = threading.Event()
    second_lock_attempted = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            assert thread_id == binding.thread_id
            return NativeSnapshot(values={"version": state["version"]})

        def close(self):
            pass

    class SignalingInvocationLockStore(InvocationLockStore):
        @contextmanager
        def hold(self, thread_id):
            second_lock_attempted.set()
            with super().hold(thread_id):
                yield

    def runtime(*, signal_lock_attempt=False):
        candidate = GraphRuntime(
            bundle_store=bundles,
            leases=leases,
            invocations=(
                SignalingInvocationLockStore(tmp_path / "owner-state")
                if signal_lock_attempt
                else InvocationLockStore(tmp_path / "owner-state")
            ),
            checkpoint_path=tmp_path / "checkpoints.sqlite",
            app_factory=lambda *_: App(),
        )
        candidate.bind(binding)
        return candidate

    first = runtime()
    second = runtime(signal_lock_attempt=True)

    def first_decision():
        with first.decision_guard(binding.public_run_id):
            observed = first.snapshot(binding.public_run_id)
            first_entered.set()
            assert release_first.wait(1)
            assert observed.values == {"version": 0}
            state["version"] = 1

    def second_decision():
        with second.decision_guard(binding.public_run_id):
            second_entered.set()
            return second.snapshot(binding.public_run_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_pending = pool.submit(first_decision)
            assert first_entered.wait(1)
            second_pending = pool.submit(second_decision)
            assert second_lock_attempted.wait(1)
            assert not second_entered.wait(0.1)
            release_first.set()
            first_pending.result(timeout=1)
            observed = second_pending.result(timeout=1)
        assert observed.values == {"version": 1}
    finally:
        release_first.set()
        first.close()
        second.close()
        store.close()


def test_engine_drive_serializes_complete_decisions_across_runtimes(tmp_path):
    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    leases = LeaseStore(store)
    first_decision = threading.Event()
    second_lock_attempted = threading.Event()
    second_snapshot = threading.Event()
    release_first = threading.Event()

    class App:
        def __init__(self, entered):
            self._entered = entered

        def snapshot(self, *, thread_id, subgraphs=False):
            assert thread_id == binding.thread_id
            self._entered.set()
            return NativeSnapshot(values={"lockstep_outcome": "PASS"})

        def close(self):
            pass

    class SignalingInvocationLockStore(InvocationLockStore):
        @contextmanager
        def hold(self, thread_id):
            second_lock_attempted.set()
            with super().hold(thread_id):
                yield

    def runtime(entered, *, signal_lock_attempt=False):
        candidate = GraphRuntime(
            bundle_store=bundles,
            leases=leases,
            invocations=(
                SignalingInvocationLockStore(tmp_path / "owner-state")
                if signal_lock_attempt
                else InvocationLockStore(tmp_path / "owner-state")
            ),
            checkpoint_path=tmp_path / "checkpoints.sqlite",
            app_factory=lambda *_: App(entered),
        )
        candidate.bind(binding)
        return candidate

    first_runtime = runtime(threading.Event())
    second_runtime = runtime(second_snapshot, signal_lock_attempt=True)

    class Catalog:
        @staticmethod
        def get(run_id):
            assert run_id == binding.public_run_id
            return binding

    class Coordinator:
        def __init__(self, *, block=False):
            self._block = block

        def reconcile_consumed(self, run_id):
            assert run_id == binding.public_run_id
            if self._block:
                first_decision.set()
                assert release_first.wait(1)
            return ()

    def drive_service(candidate, coordinator):
        return EngineDriveService(
            runtime=candidate,
            catalog=Catalog(),
            leases=leases,
            effects=object(),
            coordinator=coordinator,
            max_decisions=1,
            protected_descriptor=lambda _interrupt: None,
            reserve_effect_run=lambda _run_id: True,
            activate_effect_run=lambda _run_id: None,
            deactivate_effect_run=lambda _run_id: None,
        )

    first = drive_service(first_runtime, Coordinator(block=True))
    second = drive_service(second_runtime, Coordinator())
    stale = NativeSnapshot(values={"lockstep_outcome": "FAIL"})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_pending = pool.submit(
                first.drive, binding.public_run_id, snapshot=stale
            )
            assert first_decision.wait(1)
            second_pending = pool.submit(
                second.drive, binding.public_run_id, snapshot=stale
            )
            assert second_lock_attempted.wait(1)
            assert not second_snapshot.wait(0.1)
            release_first.set()
            first_status = first_pending.result(timeout=1)
            second_status = second_pending.result(timeout=1)
        assert first_status.status == second_status.status == "completed"
    finally:
        release_first.set()
        first_runtime.close()
        second_runtime.close()
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


@contextmanager
def _completed_child_then_parent_after_restart(tmp_path):
    bundles, binding = _binding(
        tmp_path, FIXTURES / "child_then_parent_interrupt.recipe.yaml"
    )
    store, first = _runtime(tmp_path, bundles)
    try:
        first.bind(binding)
        child, parent = _complete_child_to_parent(first, binding)
    finally:
        first.close()
        store.close()

    store, restarted = _runtime(tmp_path, bundles)
    try:
        restarted.bind(binding)
        current_parent = restarted.snapshot(
            binding.public_run_id, subgraphs=True
        ).pending[0]
        assert current_parent == parent
        proof = restarted.interrupt_lineage(binding.public_run_id, child.coordinate)
        assert proof is not None
        assert proof.disposition == "descended"
        assert proof.occurrence.coordinate == child.coordinate
        assert proof.occurrence.value == child.value
        yield binding, child, current_parent, restarted
    finally:
        restarted.close()
        store.close()


def _complete_child_to_parent(runtime, binding):
    child = runtime.start(binding.public_run_id, {}).pending[0]
    parent_snapshot = runtime.resume(
        binding.public_run_id,
        child.coordinate,
        {child.coordinate.interrupt_id: "yes"},
    )
    parent = parent_snapshot.pending[0]
    assert child.coordinate.thread_id == binding.thread_id
    assert child.coordinate.checkpoint_ns
    assert parent.coordinate.checkpoint_ns == ""
    assert child.coordinate.checkpoint_ns not in dict(parent.ancestor_checkpoints)
    assert parent_snapshot.pending == (parent,)
    return child, parent


def test_completed_child_interrupt_is_ancestor_of_live_parent_successor(tmp_path):
    bundles, binding = _binding(
        tmp_path, FIXTURES / "child_then_parent_interrupt.recipe.yaml"
    )
    store, runtime = _runtime(tmp_path, bundles)
    try:
        runtime.bind(binding)
        child, parent = _complete_child_to_parent(runtime, binding)
        assert runtime.checkpoint_is_ancestor(
            binding.public_run_id, child.coordinate, parent
        )
    finally:
        runtime.close()
        store.close()


def test_completed_child_interrupt_is_ancestor_of_parent_successor_after_restart(
    tmp_path,
):
    with _completed_child_then_parent_after_restart(tmp_path) as scenario:
        binding, child, parent, runtime = scenario
        assert runtime.checkpoint_is_ancestor(
            binding.public_run_id, child.coordinate, parent
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("thread_id", "foreign-thread"),
        ("checkpoint_id", "missing-checkpoint"),
        ("checkpoint_ns", "sibling"),
        ("task_id", "missing-task"),
        ("interrupt_id", "missing-interrupt"),
    ),
)
def test_completed_child_ancestry_rejects_inexact_source_coordinate(
    tmp_path, field, value
):
    with _completed_child_then_parent_after_restart(tmp_path) as scenario:
        binding, child, parent, runtime = scenario
        invalid = replace(child.coordinate, **{field: value})
        assert not runtime.checkpoint_is_ancestor(
            binding.public_run_id, invalid, parent
        )


def test_completed_child_ancestry_rejects_changed_current_parent_value(tmp_path):
    with _completed_child_then_parent_after_restart(tmp_path) as scenario:
        binding, child, parent, runtime = scenario
        assert not runtime.checkpoint_is_ancestor(
            binding.public_run_id,
            child.coordinate,
            replace(parent, value={"changed": True}),
        )


def test_completed_child_ancestry_rejects_noncurrent_parent_coordinate(tmp_path):
    with _completed_child_then_parent_after_restart(tmp_path) as scenario:
        binding, child, parent, runtime = scenario
        assert not runtime.checkpoint_is_ancestor(
            binding.public_run_id,
            child.coordinate,
            replace(
                parent,
                coordinate=replace(
                    parent.coordinate, checkpoint_id="not-current-checkpoint"
                ),
            ),
        )


def test_completed_child_ancestry_rejects_current_sibling_subgraph(tmp_path):
    bundles, binding = _binding(
        tmp_path, FIXTURES / "parallel_child_interrupts.recipe.yaml"
    )
    store, runtime = _runtime(tmp_path, bundles)
    try:
        runtime.bind(binding)
        parked = runtime.start(binding.public_run_id, {})
        left = next(item for item in parked.pending if item.value == "Left?")
        right = next(item for item in parked.pending if item.value == "Right?")
        assert left.coordinate.checkpoint_ns != right.coordinate.checkpoint_ns
        current = runtime.resume(
            binding.public_run_id,
            left.coordinate,
            {left.coordinate.interrupt_id: "left"},
        )
        assert current.pending == (right,)
        proof = runtime.interrupt_lineage(binding.public_run_id, left.coordinate)
        assert proof is not None
        assert proof.disposition == "descended"
        assert not runtime.checkpoint_is_ancestor(
            binding.public_run_id, left.coordinate, right
        )
    finally:
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


def test_ancestry_rejects_ambiguous_duplicate_exact_source_occurrences(tmp_path):
    from lockstep.runtime.native_models import (
        NativeCoordinate,
        NativeInterrupt,
        NativeInterruptOccurrence,
    )

    bundles, binding = _binding(tmp_path, FIXTURES / "parent_direct.recipe.yaml")
    store = SQLiteStore(tmp_path / "runtime.sqlite")
    source = NativeCoordinate(
        binding.thread_id, "source-checkpoint", "child", "source-task", "source"
    )
    current = NativeInterrupt(
        NativeCoordinate(
            binding.thread_id, "current-checkpoint", "", "current-task", "current"
        ),
        {"accept": True},
        ancestor_checkpoints=(("child", "child-anchor"),),
    )

    class App:
        def snapshot(self, *, thread_id, subgraphs=False):
            return NativeSnapshot(values={}, pending=(current,))

        def interrupt_history(self, *, thread_id, checkpoint_ns, snapshot_limit):
            occurrence = NativeInterruptOccurrence(source, {"producer": True})
            return (occurrence, occurrence)

        def checkpoint_is_ancestor(self, **_kwargs):
            return True

        def close(self):
            pass

    runtime = GraphRuntime(
        bundle_store=bundles,
        leases=LeaseStore(store),
        invocations=InvocationLockStore(tmp_path / "owner-state"),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        app_factory=lambda *_: App(),
    )
    try:
        runtime.bind(binding)
        assert not runtime.checkpoint_is_ancestor(
            binding.public_run_id, source, current
        )
    finally:
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
