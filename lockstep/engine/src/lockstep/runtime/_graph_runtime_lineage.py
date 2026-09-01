"""Bounded native lineage projection for GraphRuntime."""

from __future__ import annotations

from collections.abc import Iterable

from lockstep.runtime._graph_runtime_values import (
    MAX_HISTORY_SNAPSHOTS,
)
from lockstep.runtime.native_models import (
    NativeCoordinate,
    NativeHistoryLimitExceeded,
    NativeInterrupt,
    NativeLineageProof,
    NativeSnapshot,
)


class _GraphRuntimeLineage:
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
