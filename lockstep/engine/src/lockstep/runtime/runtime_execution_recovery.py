"""Cold reconstruction of owner-bound runtime execution composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.catalog import RunBinding, RunCatalog
from lockstep.runtime.effects.ledger import EffectLedger, EffectRecord
from lockstep.runtime.effects.owner_policy import (
    OwnerRuntimeAuthority,
    RuntimeRequirement,
    RuntimeRequirementIndex,
)
from lockstep.runtime.effects.owner_provisioning import (
    capture_runtime_execution_bindings,
)
from lockstep.runtime.effects.owner_snapshot_store import open_runtime_snapshot
from lockstep.runtime.providers.codex import CodexRunnerAdapter
from lockstep.runtime.providers.pinned import PinnedRunnerAdapter
from lockstep.runtime.recipe_bundles import RecipeBundleStore
from lockstep.runtime.runtime_execution import (
    RuntimeBundleRequirementResolver,
    RuntimeExecutionContext,
)


@dataclass(frozen=True, slots=True)
class _ProtectedRecoveryWork:
    index: RuntimeRequirementIndex
    records: tuple[tuple[EffectRecord, RuntimeRequirement], ...]


class RuntimeExecutionRecovery:
    """Reconstruct a runtime root only for durable protected work."""

    def __init__(
        self,
        *,
        state_dir: Path,
        catalog: RunCatalog,
        bundles: RecipeBundleStore,
        effects: EffectLedger,
    ) -> None:
        self._state_dir = state_dir
        self._catalog = catalog
        self._resolver = RuntimeBundleRequirementResolver(bundles)
        self._effects = effects

    @staticmethod
    def _accepted_kinds(requirement: RuntimeRequirement) -> frozenset[str]:
        if requirement.runner_selector == "codex":
            return CodexRunnerAdapter.accepted_effect_kinds
        if requirement.runner_selector == "pinned":
            return PinnedRunnerAdapter.accepted_effect_kinds
        raise ValueError("runtime requirement has an unsupported runner selector")

    def _durable_runs(
        self,
        *,
        limit: int,
        after_thread_id: str | None,
        watch_filter: Callable[[RunBinding], bool] | None = None,
    ) -> tuple[tuple[RunBinding, bool, tuple[EffectRecord, ...]], ...]:
        watches = self._watched_bindings(
            limit=limit,
            watch_filter=watch_filter,
        )
        watched = {binding.public_run_id for binding in watches}
        bindings = {binding.public_run_id: binding for binding in watches}
        records: dict[str, tuple[EffectRecord, ...]] = {}
        for thread_id in self._effects.list_recovery_threads(
            limit=limit, after_thread_id=after_thread_id
        ):
            binding = self._catalog.find_by_thread(thread_id)
            existing = bindings.get(binding.public_run_id)
            if existing is not None and existing != binding:
                raise ValueError("durable run binding changed during recovery")
            bindings[binding.public_run_id] = binding
            observed = tuple(
                self._effects.list_nonterminal_for_thread(thread_id, limit=limit + 1)
            )
            if len(observed) > limit:
                raise ValueError("run exceeds the bounded nonterminal effect capacity")
            records[binding.public_run_id] = observed
        return tuple(
            (binding, run_id in watched, records.get(run_id, ()))
            for run_id, binding in sorted(bindings.items())
        )

    def _watched_bindings(
        self,
        *,
        limit: int,
        watch_filter: Callable[[RunBinding], bool] | None,
    ) -> tuple[RunBinding, ...]:
        high_water = self._effects.max_run_drive_admission_seq()
        if high_water is None:
            return ()
        page_size = 128 if watch_filter is not None else limit
        cursor = 0
        bindings = []
        while len(bindings) < limit:
            page = self._effects.list_run_drive_watches(
                after_admission_seq=cursor,
                high_water=high_water,
                limit=page_size,
            )
            if not page:
                break
            for watch in page:
                binding = self._catalog.get(watch.public_run_id)
                if watch_filter is None or watch_filter(binding):
                    bindings.append(binding)
                    if len(bindings) == limit:
                        break
            cursor = page[-1].admission_seq
            if len(page) < page_size:
                break
        return tuple(bindings)

    def _protected_work(
        self, *, limit: int, after_thread_id: str | None
    ) -> tuple[_ProtectedRecoveryWork, ...]:
        watched_indexes: dict[str, RuntimeRequirementIndex] = {}

        def has_protected_requirements(binding: RunBinding) -> bool:
            index = self._resolver.index(binding)
            if not index.requirements:
                return False
            watched_indexes[binding.public_run_id] = index
            return True

        work = []
        for binding, watched, records in self._durable_runs(
            limit=limit,
            after_thread_id=after_thread_id,
            watch_filter=has_protected_requirements,
        ):
            index = watched_indexes.get(binding.public_run_id)
            if index is None:
                index = self._resolver.index(binding)
            matched = self._match_records(index, records)
            if (watched and index.requirements) or matched:
                work.append(_ProtectedRecoveryWork(index, matched))
        return tuple(work)

    def _match_records(
        self,
        index: RuntimeRequirementIndex,
        records: tuple[EffectRecord, ...],
    ) -> tuple[tuple[EffectRecord, RuntimeRequirement], ...]:
        protected_kinds = (
            CodexRunnerAdapter.accepted_effect_kinds
            | PinnedRunnerAdapter.accepted_effect_kinds
        )
        by_descriptor: dict[str, list[RuntimeRequirement]] = {}
        for requirement in index.requirements:
            by_descriptor.setdefault(
                requirement.protected_descriptor_digest, []
            ).append(requirement)
        matched = []
        for record in records:
            requirements = by_descriptor.get(record.descriptor_digest, [])
            if not requirements:
                if record.effect_kind in protected_kinds:
                    raise ValueError(
                        "durable protected effect is absent from immutable bundle"
                    )
                continue
            if len(requirements) != 1:
                raise ValueError(
                    "durable protected effect has no exact immutable requirement"
                )
            requirement = requirements[0]
            if record.effect_kind not in self._accepted_kinds(requirement):
                raise ValueError(
                    "durable protected effect kind differs from immutable selector"
                )
            matched.append((record, requirement))
        return tuple(matched)

    def reconstruct(
        self, *, limit: int, after_thread_id: str | None = None
    ) -> RuntimeExecutionContext | None:
        work = self._protected_work(limit=limit, after_thread_id=after_thread_id)
        if not work:
            return None
        digest, snapshot = open_runtime_snapshot(self._state_dir)
        captured = None
        for project_identity in sorted({item.index.project_identity for item in work}):
            observed = capture_runtime_execution_bindings(
                snapshot, project=Path(project_identity)
            )
            if captured is not None and observed != captured:
                raise ValueError("owner runtime bindings changed during recovery")
            captured = observed
        assert captured is not None
        authority = OwnerRuntimeAuthority(
            snapshot_digest=digest,
            snapshot=snapshot,
            codex_binding=captured.codex_facts,
            pinned_binding=captured.pinned_facts,
        )
        for item in work:
            authority.preflight(item.index)
            for record, requirement in item.records:
                expected = (
                    snapshot.codex
                    if requirement.runner_selector == "codex"
                    else snapshot.pinned
                )
                if (
                    expected is None
                    or record.runner_binding_digest != expected.binding_digest
                ):
                    raise ValueError(
                        "durable protected effect uses a different owner runner binding"
                    )
        return RuntimeExecutionContext(digest, snapshot, captured)
