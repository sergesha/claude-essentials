"""Structural ownership checks for the runtime snapshot boundary."""

from __future__ import annotations

from typing import get_type_hints


def test_snapshot_resolver_reexports_private_lineage_and_fact_owners() -> None:
    from lockstep.runtime import snapshot_resolver
    from lockstep.runtime._snapshot_facts import (
        EffectRuntimeInput,
        RuntimeSnapshotConflict as FactsConflict,
        RuntimeSnapshotFacts,
    )
    from lockstep.runtime._snapshot_lineage import (
        RuntimeSnapshotConflict,
        _chain,
        _read_regular,
        capture_authoritative_snapshot,
        resolve_lineage_snapshot,
        verify_bound_snapshot,
    )

    assert snapshot_resolver.RuntimeSnapshotConflict is RuntimeSnapshotConflict
    assert snapshot_resolver.capture_authoritative_snapshot is capture_authoritative_snapshot
    assert snapshot_resolver.verify_bound_snapshot is verify_bound_snapshot
    assert snapshot_resolver.resolve_lineage_snapshot is resolve_lineage_snapshot
    assert snapshot_resolver.EffectRuntimeInput is EffectRuntimeInput
    assert snapshot_resolver.RuntimeSnapshotFacts is RuntimeSnapshotFacts
    assert FactsConflict is RuntimeSnapshotConflict
    assert _read_regular.__module__ == "lockstep.runtime._snapshot_lineage"
    assert _chain.__module__ == "lockstep.runtime._snapshot_lineage"
    assert snapshot_resolver.RuntimeSnapshotResolver.__module__ == (
        "lockstep.runtime.snapshot_resolver"
    )
    assert get_type_hints(snapshot_resolver.RuntimeSnapshotResolver.__init__)[
        "facts"
    ] is RuntimeSnapshotFacts
