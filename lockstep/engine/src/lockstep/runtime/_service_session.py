"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lockstep.runtime import config, sessions
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.read_resources import RuntimeReadResources


class _ServiceSession:
    def _existing_run(self, run_id: str, project: str) -> RunBinding:
        try:
            binding = self.catalog.get(run_id)
        except KeyError as exc:
            raise LockstepError(f"unknown run {run_id!r}") from exc
        if Path(binding.project_identity).resolve() != Path(project).resolve():
            raise LockstepError(f"unknown run {run_id!r}")
        return binding

    def _preflight_session_readonly(
        self, run_id: str, session_id: str | None, project: str
    ) -> None:
        project_identity = str(Path(project).resolve())
        resources = RuntimeReadResources(self.state_dir)
        try:
            binding = resources.binding_for(
                run_id, project_identity
            )
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc
        if binding is None:
            raise LockstepError(f"unknown run {run_id!r}")
        try:
            session_binding = resources.session_binding(run_id)
        except Exception as exc:
            raise LockstepError(
                "trusted native state failed read-only verification"
            ) from exc
        if (
            session_binding is None
            or not isinstance(session_id, str)
            or not session_id
            or session_binding["session_id"] != session_id
            or not sessions.is_live(
                session_binding, config.session_stale_minutes()
            )
        ):
            raise LockstepError(
                "worker session binding missing, stale, or mismatched"
            )

    @contextmanager
    def _bind_existing(
        self, run_id: str, project: str
    ) -> Iterator[RunBinding]:
        with self._admission_recovery_lock:
            binding = self._existing_run(run_id, project)
            try:
                owns_binding = self.runtime.bind(binding)
            except Exception as exc:  # immutable binding cannot be reconstructed
                raise LockstepError(
                    f"run {run_id}: native binding integrity failure"
                ) from exc
            try:
                yield binding
            finally:
                self._finish_owned_effect_binding(run_id, owns_binding)

    def require_session(
        self, run_id: str, session_id: str | None, project: str
    ) -> None:
        """Fail closed at an external mutation edge; resume rechecks it too."""
        if not self._writable_core_active:
            self._preflight_session_readonly(run_id, session_id, project)
        self._activate_writable_core()
        with self._admission_recovery_lock:
            self._existing_run(run_id, project)
            self._require_session_owner(run_id, session_id)

    def _require_session_owner(
        self, run_id: str, session_id: str | None
    ) -> None:
        try:
            with sessions.locked_owner(
                self.state_dir,
                run_id,
                session_id,
                config.session_stale_minutes(),
            ):
                pass
        except PermissionError as exc:
            raise LockstepError(str(exc)) from exc
