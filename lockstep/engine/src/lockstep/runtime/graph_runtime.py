"""Thin lifecycle and coordinate guard over native checkpointed applications."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from threading import RLock

from lockstep.recipe.authority import AuthorizedMaterialization
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.invocation_lock import InvocationLockStore
from lockstep.runtime.leases import LeaseStore
from lockstep.runtime.native_models import (
    NativeAppFactory,
    NativeAppPort,
    NativeCoordinate,
    NativeEvent,
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


class NativeHistoryLimitExceeded(RuntimeError):
    """Native checkpoint history exceeds the bounded public projection."""


MAX_HISTORY_SNAPSHOTS = 1024


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
        self._closed = False

    @property
    def checkpoint_path(self) -> Path:
        return self._checkpoint_path

    def _ensure_open(self) -> None:
        if self._closed:
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
        with self._lock:
            app = self._apps.pop(run_id, None)
            self._bindings.pop(run_id, None)
        if app is not None:
            app.close()

    def _bound(self, run_id: str) -> tuple[RunBinding, NativeAppPort]:
        self._ensure_open()
        try:
            return self._bindings[run_id], self._apps[run_id]
        except KeyError as exc:
            raise KeyError(f"run {run_id!r} is not bound") from exc

    def binding(self, run_id: str) -> RunBinding:
        """Return the immutable binding used by this compiled native app."""

        binding, _app = self._bound(run_id)
        return binding

    def _invoke(
        self, run_id: str, operation: Callable[[], NativeSnapshot]
    ) -> NativeSnapshot:
        binding, _app = self._bound(run_id)
        with self._invocations.hold(binding.thread_id):
            owner = secrets.token_hex(16)
            lease = self._leases.acquire(
                "invoke", binding.thread_id, owner, self._lease_ttl
            )
            try:
                return operation()
            finally:
                self._leases.release(lease)

    def start(self, run_id: str, input: dict) -> NativeSnapshot:
        binding, app = self._bound(run_id)
        return self._invoke(
            run_id, lambda: app.invoke(dict(input), thread_id=binding.thread_id)
        )

    def snapshot(self, run_id: str, *, subgraphs: bool = False) -> NativeSnapshot:
        binding, app = self._bound(run_id)
        return app.snapshot(thread_id=binding.thread_id, subgraphs=subgraphs)

    def history(self, run_id: str) -> Iterable[NativeSnapshot]:
        binding, app = self._bound(run_id)
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

    def _lineage_contains(self, run_id: str, source: NativeCoordinate) -> bool:
        binding, app = self._bound(run_id)
        history = iter(app.history(thread_id=binding.thread_id))
        try:
            for index, snapshot in enumerate(history):
                if index >= MAX_HISTORY_SNAPSHOTS:
                    raise NativeHistoryLimitExceeded(
                        "native lineage exceeds validation limit"
                    )
                # Public history collapses a direct-subgraph interrupt into its
                # parent task coordinate.  Its interrupt ID remains stable,
                # while checkpoint namespace/task/checkpoint IDs do not.  Exact
                # full-coordinate membership is enforced on the current snapshot;
                # history is only descendant evidence in the bound thread.
                if any(
                    item.coordinate.thread_id == source.thread_id
                    and item.coordinate.interrupt_id == source.interrupt_id
                    for item in snapshot.pending
                ):
                    return True
            return False
        finally:
            close = getattr(history, "close", None)
            if close is not None:
                close()

    def coordinate_lineage(self, run_id: str, source: NativeCoordinate) -> str:
        """Classify an exact source using only public snapshot/history APIs."""

        current = self.snapshot(run_id, subgraphs=True)
        if any(item.coordinate == source for item in current.pending):
            return "pending"
        return "descended" if self._lineage_contains(run_id, source) else "incompatible"

    @staticmethod
    def _same_coordinate(left: NativeCoordinate, right: NativeCoordinate) -> bool:
        return left == right

    def resume(
        self,
        run_id: str,
        source: NativeCoordinate,
        results_by_interrupt_id: Mapping[str, object],
    ) -> NativeSnapshot:
        binding, app = self._bound(run_id)
        if source.thread_id != binding.thread_id:
            raise NativeCoordinateRejected("resume source belongs to another thread")
        supplied = set(results_by_interrupt_id)
        if not supplied:
            raise NativeCoordinateRejected(
                "resume requires at least one interrupt result"
            )

        def guarded_resume() -> NativeSnapshot:
            # Membership is checked while holding the same invocation lease
            # that covers resume, so a queued stale caller cannot advance a
            # newly exposed interrupt after the first caller commits.
            current = self.snapshot(run_id, subgraphs=True)
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
            if not self._lineage_contains(run_id, source):
                raise NativeCoordinateRejected(
                    "resume source is absent from native lineage"
                )
            return app.resume(
                thread_id=binding.thread_id,
                results_by_interrupt_id=dict(results_by_interrupt_id),
            )

        return self._invoke(run_id, guarded_resume)

    def stream(self, run_id: str, input_or_command: object) -> Iterable[NativeEvent]:
        binding, app = self._bound(run_id)

        def events() -> Iterable[NativeEvent]:
            with self._invocations.hold(binding.thread_id):
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
            if self._closed:
                return
            self._closed = True
            apps = tuple(self._apps.values())
            self._apps.clear()
            self._bindings.clear()
        first_error: BaseException | None = None
        for app in apps:
            try:
                app.close()
            except BaseException as exc:  # noqa: BLE001 - close every owner first
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
