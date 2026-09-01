"""Pure recipe admission and profile boundary."""

from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from pathlib import Path

from lockstep.authoring import (
    AuthoringError,
    CanonicalObservation,
    classify_generated_recipe_observation,
)
from lockstep.recipe import profile
from lockstep.recipe.authority import (
    AuthorizedRecipe,
    RecipeAuthorityError,
    RecipeAuthorityPolicy,
    RecipeCandidate,
)
from lockstep.recipe.loader import RecipeError, RecipeLoader
from lockstep.runtime.errors import LockstepError
from lockstep.runtime.recipe_bundles import RecipeBundleStore

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _ServiceRecipeLookup:
    def _recipe_path(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(name or ""):
            raise LockstepError(f"invalid recipe name {name!r}")
        try:
            return RecipeLoader(self.recipes_dir).resolve(name).path
        except RecipeError as exc:
            raise LockstepError(str(exc)) from exc

    def recipe_path(self, name: str) -> Path:
        return self._recipe_path(name)


def _resolve_preflight_recipe(
    recipes_dir: Path,
    name: str,
    compiler_provenance: profile.CompilerProvenance | None,
) -> tuple[RecipeCandidate, CanonicalObservation | None]:
    loader = RecipeLoader(recipes_dir)
    direct = recipes_dir / f"{name}.recipe.yaml"
    if compiler_provenance is None and (direct.exists() or direct.is_symlink()):
        observation = classify_generated_recipe_observation(
            recipes_dir, name, direct
        )
        if observation is not None:
            return observation.candidate, observation
    _ref, candidate = loader.resolve_candidate(name)
    return candidate, None

def preflight_recipe(
    recipes_dir: Path,
    name: str,
    *,
    authority_policy: RecipeAuthorityPolicy | None = None,
    compiler_provenance: profile.CompilerProvenance | None = None,
) -> AuthorizedRecipe:
    """Pure admission/profile boundary: no persistent Lockstep state exists yet."""
    if not _NAME_RE.fullmatch(name or ""):
        raise LockstepError(f"invalid recipe name {name!r}")
    try:
        candidate, canonical_observation = _resolve_preflight_recipe(
            Path(recipes_dir).resolve(), name, compiler_provenance
        )
        # A trusted same-process compiler capability already binds the exact
        # executable bundle (used by embedders/tests before files are checked
        # into the conventional project layout).  Public file ingress has no
        # such capability and must mint canonical-match from source instead.
        canonical_proof = (
            None if canonical_observation is None else canonical_observation.proof
        )
        effective_provenance = canonical_proof or compiler_provenance
        if (
            effective_provenance is not None
            and candidate.source_bundle_sha256
            != effective_provenance.source_bundle_sha256
        ):
            raise RecipeAuthorityError(
                "compiler provenance does not bind the exact source bundle"
            )
        authorized = candidate.authorize(
            authority_policy or RecipeAuthorityPolicy()
        )
        authorized = replace(
            authorized, canonical_match_proof=canonical_proof
        )
        with tempfile.TemporaryDirectory(prefix="lockstep-preflight-") as raw:
            store = RecipeBundleStore(Path(raw) / "owner-state")
            materialized = authorized.capture(store).materialize(store)
            if effective_provenance is None:
                errors, _warnings = profile.check_recipe_full(
                    materialized.source_path
                )
            else:
                errors, _warnings = profile.check_recipe_full(
                    materialized.source_path, provenance=effective_provenance
                )
    except (OSError, ValueError, AuthoringError, RecipeError, RecipeAuthorityError) as exc:
        raise LockstepError(str(exc)) from exc
    if errors:
        raise LockstepError("recipe failed Lockstep profile: " + "; ".join(errors))
    return authorized
