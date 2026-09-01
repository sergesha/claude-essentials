"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

import hashlib
import json

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.effects._coordinator_values import (
    CoordinatorLineageError,
)
from lockstep.runtime.effects.descriptors import (
    parse_effect_descriptor,
    parse_scope_result,
)
from lockstep.runtime.effects.models import (
    EffectDescriptor,
    ScopeDescriptor,
    ScopeResult,
)
from lockstep.runtime.native_models import NativeInterrupt, NativeSnapshot
from lockstep.runtime.providers.base import (
    ScopeBinding,
)


class _EffectCoordinatorLineage:
    def _binding(self, run_id: str) -> RunBinding:
        catalog_binding = self._catalog.get(run_id)
        runtime_binding = self._runtime.binding(run_id)
        if catalog_binding != runtime_binding:
            raise CoordinatorLineageError(
                "runtime binding differs from immutable run catalog lineage"
            )
        return catalog_binding

    def _ancestor_results(
        self,
        run_id: str,
        binding: RunBinding,
        descriptor: EffectDescriptor | ScopeDescriptor,
        snapshot: NativeSnapshot,
        consumer: NativeInterrupt,
    ) -> tuple[tuple[ScopeResult, ScopeBinding], ...]:
        consumer_values = (
            snapshot.values
            if consumer.state_values is None
            else consumer.state_values
        )
        keys = (
            descriptor.scope_state_keys
            if isinstance(descriptor, EffectDescriptor)
            else descriptor.ancestor_deadline_state_keys
        )
        results = []
        for key in keys:
            if key not in consumer_values:
                raise CoordinatorLineageError(
                    f"protected descriptor references absent graph state {key!r}"
                )
            result = parse_scope_result(consumer_values[key])
            try:
                producer = self._ledger.get(result.effect_id)
            except KeyError as exc:
                raise CoordinatorLineageError(
                    f"state {key!r} has no ledger-proven scope producer"
                ) from exc
            if (
                producer.effect_kind != "scope"
                or producer.phase != "delivered"
                or producer.coordinate.thread_id != binding.thread_id
                or producer.descriptor_digest != result.scope_digest
                or producer.result != result
            ):
                raise CoordinatorLineageError(
                    f"state {key!r} is not backed by a delivered scope producer"
                )
            proof = self._runtime.interrupt_lineage(run_id, producer.coordinate)
            if proof is None or proof.disposition != "descended":
                raise CoordinatorLineageError(
                    f"scope producer for state {key!r} lacks compatible native lineage"
                )
            if not self._runtime.checkpoint_is_ancestor(
                run_id, producer.coordinate, consumer
            ):
                raise CoordinatorLineageError(
                    f"scope producer for state {key!r} is not an ancestor"
                )
            producer_descriptor = parse_effect_descriptor(
                self._raw_descriptor(
                    NativeInterrupt(proof.occurrence.coordinate, proof.occurrence.value)
                )
            )
            if (
                not isinstance(producer_descriptor, ScopeDescriptor)
                or producer_descriptor.digest != producer.descriptor_digest
                or producer_descriptor.result_state_key != key
            ):
                raise CoordinatorLineageError(
                    f"scope producer does not own declared result state {key!r}"
                )
            result_json = json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            results.append(
                (
                    result,
                    ScopeBinding(
                        state_key=key,
                        producer_effect_id=producer.effect_id,
                        producer_coordinate=producer.coordinate,
                        scope_digest=result.scope_digest,
                        scope_result_digest=hashlib.sha256(result_json).hexdigest(),
                        runner_binding_digest=result.runner_binding_digest,
                    ),
                )
            )
        return tuple(results)
