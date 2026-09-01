"""The strict boundary between a recipe directory and runnable recipes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from lockstep.recipe.authority import (
    RecipeAuthorityError,
    RecipeCandidate,
    StrictRecipeIngress,
)

_SUFFIX = ".recipe.yaml"


class RecipeError(ValueError):
    """A recipe reference is outside the recipe root or is malformed."""


@dataclass(frozen=True)
class RecipeRef:
    name: str
    path: Path
    kind: Literal["manual", "generated"]
    definition_sha256: str


class RecipeLoader:
    def __init__(self, recipes_dir: Path) -> None:
        self._root = Path(recipes_dir).resolve()

    def _inspect_path(self, path: Path) -> tuple[RecipeCandidate, str, Path]:
        if path.name.endswith(_SUFFIX) is False:
            raise RecipeError(f"recipe path must end in {_SUFFIX}: {path}")
        absolute = path if path.is_absolute() else path.absolute()
        try:
            logical = absolute.relative_to(self._root).as_posix()
        except ValueError:
            raise RecipeError(f"recipe path escapes recipe directory: {path}")
        try:
            candidate = StrictRecipeIngress(self._root).inspect(logical)
        except (OSError, RecipeAuthorityError, ValueError) as exc:
            raise RecipeError(str(exc)) from exc
        return candidate, logical, self._root / logical

    def _ref_for_inspection(
        self,
        candidate: RecipeCandidate,
        logical: str,
        canonical_path: Path,
    ) -> RecipeRef:
        doc = json.loads(
            next(item.bytes for item in candidate.files if item.path == logical)
        )
        name = canonical_path.name.removesuffix(_SUFFIX)
        if doc.get("name") != name:
            raise RecipeError(
                f"recipe document name must equal filename {name!r}: {canonical_path}"
            )
        kind: Literal["manual", "generated"] = (
            "generated"
            if isinstance(doc.get("x-lockstep-generated"), dict)
            else "manual"
        )
        return RecipeRef(
            name=name,
            path=canonical_path,
            kind=kind,
            definition_sha256=candidate.definition_sha256,
        )

    def _ref_for_path(self, path: Path) -> RecipeRef:
        candidate, logical, canonical_path = self._inspect_path(path)
        return self._ref_for_inspection(candidate, logical, canonical_path)

    def _candidate_ref_for_path(
        self, path: Path
    ) -> tuple[RecipeCandidate, RecipeRef]:
        candidate, logical, canonical_path = self._inspect_path(path)
        return candidate, self._ref_for_inspection(candidate, logical, canonical_path)

    def discover(self) -> dict[str, RecipeRef]:
        if not self._root.exists():
            return {}
        discovered: dict[str, RecipeRef] = {}
        for path in sorted(self._root.rglob(f"*{_SUFFIX}")):
            ref = self._ref_for_path(path)
            if ref.name in discovered:
                raise RecipeError(f"duplicate recipe name {ref.name!r}")
            discovered[ref.name] = ref
        return discovered

    def resolve(self, name_or_path: str | Path) -> RecipeRef:
        candidate = Path(name_or_path)
        if candidate.is_absolute() or candidate.parent != Path("."):
            return self._ref_for_path(candidate)
        if str(name_or_path).endswith(_SUFFIX):
            return self._ref_for_path(self._root / candidate)
        direct = self._root / f"{name_or_path}{_SUFFIX}"
        if direct.exists() or direct.is_symlink():
            # Resolve the named authority root without first treating every
            # transitive ``*.recipe.yaml`` dependency as a public root.
            return self._ref_for_path(direct)
        try:
            return self.discover()[str(name_or_path)]
        except KeyError as exc:
            raise RecipeError(
                f"recipe not found: {name_or_path!r}; runnable recipes end in {_SUFFIX}"
            ) from exc

    def resolve_candidate(
        self, name_or_path: str | Path
    ) -> tuple[RecipeRef, RecipeCandidate]:
        """Resolve a reference while retaining its same-pass ingress candidate."""

        candidate_path = Path(name_or_path)
        if candidate_path.is_absolute() or candidate_path.parent != Path("."):
            candidate, ref = self._candidate_ref_for_path(candidate_path)
            return ref, candidate
        if str(name_or_path).endswith(_SUFFIX):
            candidate, ref = self._candidate_ref_for_path(
                self._root / candidate_path
            )
            return ref, candidate
        direct = self._root / f"{name_or_path}{_SUFFIX}"
        if direct.exists() or direct.is_symlink():
            candidate, ref = self._candidate_ref_for_path(direct)
            return ref, candidate
        ref = self.resolve(name_or_path)
        candidate, verified = self._candidate_ref_for_path(ref.path)
        if verified != ref:
            raise RecipeError(f"recipe reference changed while resolving: {ref.path}")
        return ref, candidate

    def load(self, ref: RecipeRef) -> dict[str, Any]:
        candidate, logical, canonical_path = self._inspect_path(ref.path)
        verified = self._ref_for_inspection(candidate, logical, canonical_path)
        if verified != ref:
            raise RecipeError(f"recipe reference changed while loading: {ref.path}")
        return json.loads(
            next(item.bytes for item in candidate.files if item.path == logical)
        )
