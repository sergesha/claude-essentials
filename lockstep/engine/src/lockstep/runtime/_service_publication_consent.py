"""Capability owner extracted from the command-service facade."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lockstep.runtime.effects.authority import (
    EffectAuthorityDenied,
)
from lockstep.runtime.effects.owner_consent import (
    IssuedPublicationConsent,
)
from lockstep.runtime.errors import LockstepError


class _ServicePublicationConsent:
    def preview_publication_consent(
        self, run_id: str, step: str, *, project: str
    ) -> dict[str, Any]:
        self._activate_writable_core()
        with self._bind_existing(run_id, project):
            _binding, interrupt = self._pending_acceptance(
                run_id, step, project=project
            )
            return self.coordinator.preview_acceptance(
                run_id, interrupt.coordinate
            ).to_dict()

    def issue_publication_consent(
        self,
        run_id: str,
        step: str,
        expected_commitment_digest: str,
        *,
        project: str,
    ) -> IssuedPublicationConsent:
        self._activate_writable_core()
        with self._admission_recovery_lock, self._bind_existing(run_id, project):
            _binding, interrupt = self._pending_acceptance(
                run_id, step, project=project
            )
            return self.coordinator.issue_acceptance_consent(
                run_id,
                interrupt.coordinate,
                expected_commitment_digest,
            )

    def scenario_accept_artifact(
        self, token: str, *, project: str
    ) -> dict[str, Any]:
        """Redeem one owner bearer token within the ambient host project."""

        project_identity = str(Path(project).resolve())
        self._activate_writable_core()
        try:
            stored = self.authority.inspect_token(token)
        except (EffectAuthorityDenied, TypeError, ValueError) as exc:
            raise LockstepError("invalid or stale publication consent") from exc
        commitment = stored.commitment
        if commitment.project_identity != project_identity:
            raise LockstepError("invalid or stale publication consent")
        with self._admission_recovery_lock, self._bind_existing(
            commitment.public_run_id, project_identity
        ) as binding:
            try:
                if (
                    binding.public_run_id != commitment.public_run_id
                    or binding.project_identity != commitment.project_identity
                    or binding.recipe_digest != commitment.definition_digest
                ):
                    raise LockstepError("invalid or stale publication consent")
                self.coordinator.submit_acceptance(
                    commitment.public_run_id,
                    commitment.source,
                    token,
                )
            except EffectAuthorityDenied as exc:
                raise LockstepError("invalid or stale publication consent") from exc
            return self._drive_engine_owned(
                commitment.public_run_id, binding=binding
            ).to_dict()

    def revoke_publication_consents(self, *, project: str) -> int:
        self._activate_writable_core()
        return self.authority.revoke(str(Path(project).resolve()))
