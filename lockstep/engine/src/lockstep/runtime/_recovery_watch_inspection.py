"""Snapshot adoption and descriptor inspection for one recovery watch."""

from __future__ import annotations

from lockstep.runtime.blobs import BlobRef
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.descriptors import parse_effect_descriptor
from lockstep.runtime.effects.ledger import RunDriveWatch
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.start_input import decode_canonical_start_input


class _RecoveryWatchInspection:
    def _snapshot_for_run_drive_watch(
        self, binding: RunBinding, watch: RunDriveWatch
    ) -> NativeSnapshot | None:
        snapshot = self._runtime.snapshot(watch.public_run_id, subgraphs=True)
        if snapshot.checkpoint_id:
            return snapshot
        if watch.input_blob_sha256 is None or watch.input_blob_size is None:
            return None
        self._snapshot_resolver.start_ref(binding)
        encoded = self._blobs.read(
            BlobRef(watch.input_blob_sha256, watch.input_blob_size)
        )
        return self._runtime.ensure_started(
            watch.public_run_id,
            decode_canonical_start_input(encoded),
        )

    @staticmethod
    def _pending_run_drive_descriptor(
        snapshot: NativeSnapshot,
    ) -> tuple[NativeInterrupt, object] | None:
        if len(snapshot.pending) != 1:
            return None
        interrupt = snapshot.pending[0]
        raw = (
            interrupt.value.get("lockstep_effect")
            if isinstance(interrupt.value, dict)
            else None
        )
        try:
            descriptor = parse_effect_descriptor(raw)
        except (TypeError, ValueError):
            return None
        return interrupt, descriptor
