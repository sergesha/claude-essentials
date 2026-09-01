"""Invocation and commitment guard for GraphRuntime."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager

from lockstep.runtime._graph_runtime_values import (
    MAX_HISTORY_INTERRUPTS,
    MAX_HISTORY_SNAPSHOTS,
    NativeCommitment,
    NativeCoordinateRejected,
    RuntimeBindingConflict,
)
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import (
    NativeAppPort,
    NativeCoordinate,
    NativeEvent,
    NativeHistoryLimitExceeded,
    NativeInterruptOccurrence,
    NativeLineageProof,
    NativeSnapshot,
)


class _GraphRuntimeGuard:
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

    @contextmanager
    def decision_guard(self, run_id: str) -> Iterator[None]:
        """Serialize one complete snapshot-to-decision cycle for a run."""

        with self._app_guard(run_id):
            yield

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
