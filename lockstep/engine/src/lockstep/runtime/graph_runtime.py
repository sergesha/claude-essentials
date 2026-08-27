"""Thin lifecycle and coordinate guard over native checkpointed applications."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock, local

from lockstep.recipe.authority import AuthorizedMaterialization
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.native_models import (
    NativeAppFactory,
    NativeAppPort,
    NativeCoordinate,
    NativeEvent,
    NativeHistoryLimitExceeded,
    NativeInterrupt,
    NativeInterruptOccurrence,
    NativeLineageProof,
    NativeSnapshot,
)
from lockstep.runtime.recipe_bundles import (
    RecipeBundleRef,
    RecipeBundleStore,
    ValidatedDependencyDAG,
)


class NativeCoordinateRejected(ValueError):
    """A resume source is stale, foreign, or no longer pending."""


class RuntimeBindingConflict(RuntimeError):
    """A public run is already bound to a different immutable identity."""


MAX_HISTORY_SNAPSHOTS = 1024
MAX_HISTORY_INTERRUPTS = 4096


@dataclass(frozen=True)
class NativeCommitment:
    """Exact graph-owned facts observed under native invocation serialization."""

    binding: RunBinding
    snapshot: NativeSnapshot
    interrupt: NativeInterrupt


class GraphRuntime:
    """Keep native apps alive while leaving all workflow state in checkpoints."""

    def __init__(
        self,
        *,
        bundle_store: RecipeBundleStore,
        leases: LeaseStore,
        invocations: InvocationLockStore,
        checkpoint_path: Path,
        app_factory: NativeAppFactory,
        lease_ttl: float = 60.0,
    ) -> None:
        self._bundles = bundle_store
        self._leases = leases
        self._checkpoint_path = Path(checkpoint_path)
        self._app_factory = app_factory
        self._lease_ttl = lease_ttl
        self._invocations = invocations
        self._bindings: dict[str, RunBinding] = {}
        self._apps: dict[str, NativeAppPort] = {}
        self._lock = RLock()
        self._guard_local = local()
        self._closed = False
        self._closing = False

    @property
    def checkpoint_path(self) -> Path:
        return self._checkpoint_path

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("GraphRuntime is closed")

    def bind(self, run: RunBinding) -> None:
        self._ensure_open()
        with self._lock:
            current = self._bindings.get(run.public_run_id)
            if current is not None:
                if (
                    current.public_run_id,
                    current.thread_id,
                    current.recipe_digest,
                    current.recipe_snapshot_ref,
                    current.project_identity,
                ) != (
                    run.public_run_id,
                    run.thread_id,
                    run.recipe_digest,
                    run.recipe_snapshot_ref,
                    run.project_identity,
                ):
                    raise RuntimeBindingConflict(
                        f"run {run.public_run_id!r} is already bound differently"
                    )
                return
            ref = RecipeBundleRef(run.recipe_snapshot_ref)
            manifest = self._bundles.read_manifest(ref)
            dag = ValidatedDependencyDAG(
                manifest.root, tuple(entry.path for entry in manifest.files)
            )
            materialized = self._bundles.materialize_for_compile(ref)
            authority = AuthorizedMaterialization(
                bundle=ref,
                definition_sha256=run.recipe_digest,
                dependency_dag=dag,
                source_path=materialized.source_path,
                directory=materialized.directory,
            )
            app = self._app_factory(authority, self._checkpoint_path)
            self._bindings[run.public_run_id] = run
            self._apps[run.public_run_id] = app

    def unbind(self, run_id: str) -> None:
        # Closing/removing a native app is itself a lifecycle mutation.  It
        # must serialize with resume, commitment, and lineage verification;
        # otherwise recovery can unbind between a committed resume and the
        # coordinator's proof that the commit descended from its source.
        with self._lock:
            binding = self._bindings.get(run_id)
        if binding is None:
            return
        with self._invocations.hold(binding.thread_id):
            with self._lock:
                if self._bindings.get(run_id) != binding:
                    return
                app = self._apps.pop(run_id, None)
                self._bindings.pop(run_id, None)
        if app is not None:
            app.close()

    def _bound(self, run_id: str) -> tuple[RunBinding, NativeAppPort]:
        with self._lock:
            self._ensure_open()
            try:
                return self._bindings[run_id], self._apps[run_id]
            except KeyError as exc:
                raise KeyError(f"run {run_id!r} is not bound") from exc

    @contextmanager
    def _app_guard(self, run_id: str) -> Iterator[tuple[RunBinding, NativeAppPort]]:
        """Serialize one app use with unbind, then revalidate after waiting."""

        nested = getattr(self._guard_local, "current", None)
        if nested is not None:
            nested_run_id, expected, app = nested
            if nested_run_id != run_id or self._bound(run_id) != (expected, app):
                raise RuntimeBindingConflict(
                    "nested native app use differs from its lifecycle guard"
                )
            yield expected, app
            return
        expected, _app = self._bound(run_id)
        with self._invocations.hold(expected.thread_id):
            binding, app = self._bound(run_id)
            if binding != expected:
                raise RuntimeBindingConflict(
                    "run binding changed while waiting for native lifecycle guard"
                )
            self._guard_local.current = (run_id, binding, app)
            try:
                yield binding, app
            finally:
                del self._guard_local.current

    def binding(self, run_id: str) -> RunBinding:
        """Return the immutable binding used by this compiled native app."""

        binding, _app = self._bound(run_id)
        return binding

    def _invoke(
        self,
        run_id: str,
        operation: Callable[[RunBinding, NativeAppPort], NativeSnapshot],
    ) -> NativeSnapshot:
        with self._app_guard(run_id) as (binding, app):
            owner = secrets.token_hex(16)
            lease = self._leases.acquire(
                "invoke", binding.thread_id, owner, self._lease_ttl
            )
            try:
                return operation(binding, app)
            finally:
                self._leases.release(lease)

    def start(self, run_id: str, input: dict) -> NativeSnapshot:
        return self.ensure_started(run_id, input)

    def ensure_started(self, run_id: str, input: dict) -> NativeSnapshot:
        """Deliver one admitted initial command, or adopt its committed checkpoint."""

        def snapshot_then_start(
            binding: RunBinding, app: NativeAppPort
        ) -> NativeSnapshot:
            current = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            if current.checkpoint_id:
                return current
            if (
                current.values
                or current.pending
                or current.next
                or current.task_errors
                or current.created_at is not None
            ):
                raise RuntimeError(
                    "native start state is present without a checkpoint identity"
                )
            return app.invoke(dict(input), thread_id=binding.thread_id)

        return self._invoke(run_id, snapshot_then_start)

    def snapshot(self, run_id: str, *, subgraphs: bool = False) -> NativeSnapshot:
        with self._app_guard(run_id) as (binding, app):
            return app.snapshot(thread_id=binding.thread_id, subgraphs=subgraphs)

    @contextmanager
    def commitment_guard(
        self, run_id: str, source: NativeCoordinate
    ) -> Iterator[NativeCommitment]:
        """Hold native commit serialization while one exact effect may launch."""

        with self._app_guard(run_id) as (binding, app):
            if source.thread_id != binding.thread_id:
                raise NativeCoordinateRejected(
                    "commitment source belongs to another native thread"
                )
            owner = secrets.token_hex(16)
            lease = self._leases.acquire(
                "invoke", binding.thread_id, owner, self._lease_ttl
            )
            try:
                snapshot = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
                matches = tuple(
                    interrupt
                    for interrupt in snapshot.pending
                    if interrupt.coordinate == source
                )
                if len(matches) != 1:
                    raise NativeCoordinateRejected(
                        "commitment source is not the exact current interrupt"
                    )
                yield NativeCommitment(binding, snapshot, matches[0])
            finally:
                self._leases.release(lease)

    def history(self, run_id: str) -> Iterable[NativeSnapshot]:
        with self._app_guard(run_id) as (binding, app):
            snapshots = []
            history = iter(app.history(thread_id=binding.thread_id))
            try:
                for index, snapshot in enumerate(history):
                    if index >= MAX_HISTORY_SNAPSHOTS:
                        raise NativeHistoryLimitExceeded(
                            "native history exceeds public projection limit"
                        )
                    snapshots.append(snapshot)
            finally:
                close = getattr(history, "close", None)
                if close is not None:
                    close()
        return tuple(snapshots)

    def interrupt_lineage(
        self, run_id: str, source: NativeCoordinate
    ) -> NativeLineageProof | None:
        """Prove one exact occurrence via current or namespace-scoped history."""

        with self._app_guard(run_id) as (binding, app):
            return self._interrupt_lineage(binding, app, source)

    def _interrupt_lineage(
        self, binding: RunBinding, app: NativeAppPort, source: NativeCoordinate
    ) -> NativeLineageProof | None:
        if source.thread_id != binding.thread_id:
            return None
        current = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
        current_matches = tuple(
            interrupt for interrupt in current.pending if interrupt.coordinate == source
        )
        if len(current_matches) == 1:
            interrupt = current_matches[0]
            return NativeLineageProof(
                "pending",
                NativeInterruptOccurrence(interrupt.coordinate, interrupt.value),
            )
        if current_matches:
            return None
        history = iter(
            app.interrupt_history(
                thread_id=binding.thread_id,
                checkpoint_ns=source.checkpoint_ns,
                snapshot_limit=MAX_HISTORY_SNAPSHOTS,
            )
        )
        matches: list[NativeInterruptOccurrence] = []
        try:
            for index, occurrence in enumerate(history):
                if index >= MAX_HISTORY_INTERRUPTS:
                    raise NativeHistoryLimitExceeded(
                        "native lineage exceeds validation limit"
                    )
                if occurrence.coordinate == source:
                    matches.append(occurrence)
        except ValueError:
            return None
        finally:
            close = getattr(history, "close", None)
            if close is not None:
                close()
        if len(matches) != 1:
            return None
        return NativeLineageProof("descended", matches[0])

    def coordinate_lineage(self, run_id: str, source: NativeCoordinate) -> str:
        """Classify an exact source using only public snapshot/history APIs."""

        proof = self.interrupt_lineage(run_id, source)
        return "incompatible" if proof is None else proof.disposition

    def checkpoint_is_ancestor(
        self,
        run_id: str,
        ancestor: NativeCoordinate,
        descendant: NativeInterrupt,
    ) -> bool:
        """Prove producer checkpoint ancestry to one exact current interrupt."""

        with self._app_guard(run_id) as (binding, app):
            if (
                ancestor.thread_id != binding.thread_id
                or descendant.coordinate.thread_id != binding.thread_id
            ):
                return False
            current = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            exact = tuple(
                item
                for item in current.pending
                if item.coordinate == descendant.coordinate
                and item.value == descendant.value
            )
            if len(exact) != 1:
                return False
            if self._interrupt_lineage(binding, app, ancestor) is None:
                return False
            anchors = dict(exact[0].ancestor_checkpoints)
            descendant_checkpoint_id = anchors.get(ancestor.checkpoint_ns)
            descendant_checkpoint_ns = ancestor.checkpoint_ns
            if ancestor.checkpoint_ns == descendant.coordinate.checkpoint_ns:
                descendant_checkpoint_id = descendant.coordinate.checkpoint_id
            elif not descendant_checkpoint_id:
                descendant_checkpoint_ns = descendant.coordinate.checkpoint_ns
                descendant_checkpoint_id = descendant.coordinate.checkpoint_id
            if not descendant_checkpoint_id:
                return False
            return app.checkpoint_is_ancestor(
                thread_id=binding.thread_id,
                ancestor_checkpoint_ns=ancestor.checkpoint_ns,
                ancestor_checkpoint_id=ancestor.checkpoint_id,
                descendant_checkpoint_ns=descendant_checkpoint_ns,
                descendant_checkpoint_id=descendant_checkpoint_id,
                snapshot_limit=MAX_HISTORY_SNAPSHOTS,
            )

    @staticmethod
    def _same_coordinate(left: NativeCoordinate, right: NativeCoordinate) -> bool:
        return left == right

    def resume(
        self,
        run_id: str,
        source: NativeCoordinate,
        results_by_interrupt_id: Mapping[str, object],
    ) -> NativeSnapshot:
        supplied = set(results_by_interrupt_id)
        if not supplied:
            raise NativeCoordinateRejected(
                "resume requires at least one interrupt result"
            )

        def guarded_resume(binding: RunBinding, app: NativeAppPort) -> NativeSnapshot:
            if source.thread_id != binding.thread_id:
                raise NativeCoordinateRejected("resume source belongs to another thread")
            # Membership is checked while holding the same invocation lease
            # that covers resume, so a queued stale caller cannot advance a
            # newly exposed interrupt after the first caller commits.
            current = app.snapshot(thread_id=binding.thread_id, subgraphs=True)
            current_by_id = {
                interrupt.coordinate.interrupt_id: interrupt.coordinate
                for interrupt in current.pending
            }
            observed = current_by_id.get(source.interrupt_id)
            if observed is None or not self._same_coordinate(observed, source):
                raise NativeCoordinateRejected(
                    "resume source is stale or no longer pending"
                )
            unknown = supplied - current_by_id.keys()
            if unknown:
                raise NativeCoordinateRejected(
                    f"interrupt result is not currently pending: {sorted(unknown)}"
                )
            proof = self._interrupt_lineage(binding, app, source)
            if proof is None:
                raise NativeCoordinateRejected(
                    "resume source is absent from native lineage"
                )
            return app.resume(
                thread_id=binding.thread_id,
                results_by_interrupt_id=dict(results_by_interrupt_id),
            )

        return self._invoke(run_id, guarded_resume)

    def stream(self, run_id: str, input_or_command: object) -> Iterable[NativeEvent]:
        def events() -> Iterable[NativeEvent]:
            with self._app_guard(run_id) as (binding, app):
                owner = secrets.token_hex(16)
                lease = self._leases.acquire(
                    "invoke", binding.thread_id, owner, self._lease_ttl
                )
                try:
                    yield from app.stream(input_or_command, thread_id=binding.thread_id)
                finally:
                    self._leases.release(lease)

        return events()

    def close(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            run_ids = tuple(self._bindings)
        first_error: BaseException | None = None
        for run_id in run_ids:
            try:
                self.unbind(run_id)
            except BaseException as exc:  # noqa: BLE001 - close every owner first
                first_error = first_error or exc
        with self._lock:
            self._closed = True
            self._closing = False
        if first_error is not None:
            raise first_error

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
