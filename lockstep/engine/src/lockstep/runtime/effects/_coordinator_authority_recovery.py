"""Crash-safe reconciliation of protected native interrupts and external attempts."""

from __future__ import annotations

from lockstep.runtime.effects._coordinator_values import (
    ProviderContractViolation,
    ReconcileReport,
)
from lockstep.runtime.effects.ledger import (
    EffectRecord,
)
from lockstep.runtime.leases import Lease
from lockstep.runtime.providers.base import (
    RunnerObservation,
)


class _EffectCoordinatorAuthorityRecovery:
    def _authority_blocked_observation(
        self, *, record: EffectRecord
    ) -> RunnerObservation:
        runner = self._runner_for_binding(record.runner_binding_digest)
        observation = runner.inspect(record.effect_id)
        self._check_observation(record, observation)
        return observation

    def _commit_authority_blocked_observation(
        self,
        *,
        run_id: str,
        record: EffectRecord,
        observation: RunnerObservation,
        lease: Lease,
    ) -> ReconcileReport:
        if observation.state == "absent":
            return self._report(run_id, record, "authority_blocked")
        if observation.state == "indeterminate":
            indeterminate = self._ledger.mark_indeterminate(
                record.effect_id,
                expected_revision=record.revision,
                lease=lease,
            )
            return self._report(run_id, indeterminate, "indeterminate")
        if observation.state not in {"running", "terminal"}:
            raise ProviderContractViolation("unknown launch observation state")
        running = self._ledger.mark_running(
            record.effect_id,
            expected_revision=record.revision,
            lease=lease,
            runner_binding_digest=record.runner_binding_digest,
        )
        return self._report(run_id, running, "running")
