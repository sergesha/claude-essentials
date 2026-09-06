"""Durable runtime-owned project snapshot inputs.

These facts are deliberately outside LangGraph state and the external-effect
ledger.  They bind exact native coordinates to immutable content-addressed
project snapshots before any authority-bearing runner port is consulted.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from lockstep.runtime._snapshot_facts import (
    EffectRuntimeInput as EffectRuntimeInput,
    RuntimeSnapshotFacts,
    _CURRENT,
    _RUN_START,
    _SUCCESSOR,
)
from lockstep.runtime._snapshot_lineage import (
    RuntimeSnapshotConflict,
    _chain,
    _read_regular as _read_regular,
    capture_authoritative_snapshot,
    merge_lineage_snapshots,
    resolve_lineage_snapshot as resolve_lineage_snapshot,
    verify_bound_snapshot,
)
from lockstep.runtime.blobs import BlobStore
from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects.models import (
    DecisionDescriptor,
    DecisionResult,
    EffectDescriptor,
    RuntimeInputSelector,
)
from lockstep.runtime.native_models import NativeInterrupt
from lockstep.runtime.project_snapshots import (
    ProjectSnapshotRef,
    ProjectSnapshotStore,
)


class RuntimeSnapshotResolver:
    def __init__(
        self,
        facts: RuntimeSnapshotFacts,
        snapshots: ProjectSnapshotStore,
        blobs: BlobStore,
        runtime,
    ) -> None:
        self._facts = facts
        self._snapshots = snapshots
        self._blobs = blobs
        self._runtime = runtime

    def start_ref(self, binding: RunBinding) -> ProjectSnapshotRef:
        ref = self._facts.run_start(binding)
        snapshot = verify_bound_snapshot(ref, self._snapshots, binding)
        if snapshot.previous is not None or snapshot.provenance["purpose"] != "run-start":
            raise RuntimeSnapshotConflict("run-start snapshot is not a lineage root")
        return ref

    def _verify_chain_binding(
        self, ref: ProjectSnapshotRef, binding: RunBinding
    ) -> None:
        chain = _chain(ref, self._snapshots)
        if self.start_ref(binding) not in chain:
            raise RuntimeSnapshotConflict(
                "runtime snapshot chain does not descend from the exact run start"
            )
        for ancestor in chain:
            snapshot = self._snapshots.read(ancestor)
            if snapshot.provenance.get("schema") == "lockstep.run-project-snapshot/v1":
                verify_bound_snapshot(ancestor, self._snapshots, binding)

    def _current_ref(
        self, binding: RunBinding, interrupt: NativeInterrupt
    ) -> ProjectSnapshotRef:
        candidates = []
        for fact in self._facts.list_successors(binding):
            self._verify_chain_binding(fact.snapshot_ref, binding)
            if self._runtime.checkpoint_is_ancestor(
                binding.public_run_id, fact.coordinate, interrupt
            ):
                candidates.append(fact.snapshot_ref)
        if not candidates:
            return self.start_ref(binding)
        chains = {ref: set(_chain(ref, self._snapshots)[1:]) for ref in candidates}
        tips = tuple(
            ref for ref in dict.fromkeys(candidates)
            if not any(ref in ancestors for other, ancestors in chains.items() if other != ref)
        )
        return merge_lineage_snapshots(tips, self._snapshots, binding)

    def inputs_for(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor | DecisionDescriptor,
        effect_id: str,
    ) -> dict[str, str]:
        selectors = tuple(
            (name, selector)
            for name, selector in descriptor.inputs
            if isinstance(selector, RuntimeInputSelector)
        )
        if not selectors:
            return {}
        try:
            bound = self._facts.get_effect(effect_id, _CURRENT)
        except KeyError:
            bound = None
        if bound is not None:
            if (
                bound.public_run_id != binding.public_run_id
                or bound.coordinate != interrupt.coordinate
                or bound.descriptor_digest != descriptor.digest
            ):
                raise RuntimeSnapshotConflict("effect runtime input belongs to foreign lineage")
            current = bound.snapshot_ref
            self._verify_chain_binding(current, binding)
        else:
            current = self._current_ref(binding, interrupt)
            self._facts.bind_effect(
                effect_id,
                _CURRENT,
                binding,
                interrupt.coordinate,
                descriptor.digest,
                current,
            )
        start = self.start_ref(binding)
        return {
            name: "snapshot:" + (start if selector.runtime_key == _RUN_START else current).digest
            for name, selector in selectors
        }

    def capture_successor(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: EffectDescriptor | object,
        effect_id: str,
        *,
        purpose: str,
    ) -> ProjectSnapshotRef:
        try:
            existing = self._facts.get_effect(effect_id, _SUCCESSOR)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.public_run_id != binding.public_run_id
                or existing.coordinate != interrupt.coordinate
                or existing.descriptor_digest != descriptor.digest
            ):
                raise RuntimeSnapshotConflict("effect successor belongs to foreign lineage")
            verify_bound_snapshot(existing.snapshot_ref, self._snapshots, binding)
            return existing.snapshot_ref
        previous = self._current_ref(binding, interrupt)
        ref = capture_authoritative_snapshot(
            Path(binding.project_identity),
            self._snapshots,
            self._blobs,
            binding,
            previous=previous,
            purpose=purpose,
            writes=descriptor.writes if (
                isinstance(descriptor, EffectDescriptor) and descriptor.parallel is not None
            ) else None,
        )
        return self._facts.bind_effect(
            effect_id,
            _SUCCESSOR,
            binding,
            interrupt.coordinate,
            descriptor.digest,
            ref,
        )

    def adopt_successor(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: object,
        effect_id: str,
        ref: ProjectSnapshotRef,
    ) -> ProjectSnapshotRef:
        """Bind a runner-produced immutable rollover after proving its input edge."""

        try:
            existing = self._facts.get_effect(effect_id, _SUCCESSOR)
        except KeyError:
            existing = None
        if existing is not None:
            if existing.snapshot_ref != ref:
                raise RuntimeSnapshotConflict(
                    "effect successor is already bound to another snapshot"
                )
            self._verify_chain_binding(ref, binding)
            return ref
        previous = self._current_ref(binding, interrupt)
        snapshot = self._snapshots.read(ref)
        if snapshot.previous != previous:
            raise RuntimeSnapshotConflict(
                "effect successor does not descend from its exact runtime input"
            )
        self._verify_chain_binding(ref, binding)
        return self._facts.bind_effect(
            effect_id,
            _SUCCESSOR,
            binding,
            interrupt.coordinate,
            descriptor.digest,
            ref,
        )

    def decide(
        self,
        binding: RunBinding,
        interrupt: NativeInterrupt,
        descriptor: DecisionDescriptor,
        effect_id: str,
    ) -> DecisionResult:
        inputs = self.inputs_for(binding, interrupt, descriptor, effect_id)
        start = self._snapshots.read(
            ProjectSnapshotRef(inputs["start_snapshot"].removeprefix("snapshot:"))
        )
        current = self._snapshots.read(
            ProjectSnapshotRef(inputs["current_snapshot"].removeprefix("snapshot:"))
        )
        before = {item.path: item.blob.sha256 for item in start.files}
        after = {item.path: item.blob.sha256 for item in current.files}
        changed = tuple(sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path)))
        value = descriptor.decision.default
        for case in descriptor.decision.cases:
            if any(
                fnmatch.fnmatchcase(path, pattern)
                or (pattern.endswith("/**") and path == pattern[:-3])
                for path in changed
                for pattern in case.paths
            ):
                value = case.label
                break
        return DecisionResult(
            "lockstep.decision-result/v1",
            effect_id,
            "PASS",
            descriptor.digest,
            value=value,
        )
