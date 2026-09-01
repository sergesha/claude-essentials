"""Native application lifecycle registry for GraphRuntime."""

from __future__ import annotations

from pathlib import Path

from lockstep.recipe.authority import AuthorizedMaterialization
from lockstep.runtime._graph_runtime_values import RuntimeBindingConflict
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import NativeAppPort
from lockstep.runtime.recipe_bundles import RecipeBundleRef, ValidatedDependencyDAG


class _GraphRuntimeLifecycle:
    @property
    def checkpoint_path(self) -> Path:
        return self._checkpoint_path

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("GraphRuntime is closed")

    def bind(self, run: RunBinding) -> bool:
        """Bind an app and report whether this call created its lifecycle."""

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
                return False
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
            return True

    def unbind(self, run_id: str) -> None:
        # Closing/removing a native app is itself a lifecycle mutation.  It
        # must serialize with resume, commitment, and lineage verification;
        # otherwise recovery can unbind between a committed resume and the
        # coordinator's proof that the commit descended from its source.
        with self._lock:
            binding = self._bindings.get(run_id)
        if binding is None:
            return
        with self._invocations.hold(binding.thread_id):  # noqa: SIM117
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

    def binding(self, run_id: str) -> RunBinding:
        """Return the immutable binding used by this compiled native app."""

        binding, _app = self._bound(run_id)
        return binding

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
