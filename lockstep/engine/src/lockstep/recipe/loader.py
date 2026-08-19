"""The strict boundary between a recipe directory and runnable recipes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


_SUFFIX = ".recipe.yaml"


class RecipeError(ValueError):
    """A recipe reference is outside the recipe root or is malformed."""


@dataclass(frozen=True)
class RecipeRef:
    name: str
    path: Path
    kind: Literal["manual", "generated"]


class RecipeLoader:
    def __init__(self, recipes_dir: Path) -> None:
        self._root = Path(recipes_dir).resolve()

    def _inside_root(self, path: Path) -> bool:
        return path == self._root or self._root in path.parents

    def _ref_for_path(self, path: Path) -> RecipeRef:
        if path.name.endswith(_SUFFIX) is False:
            raise RecipeError(f"recipe path must end in {_SUFFIX}: {path}")
        resolved = path.resolve()
        if not self._inside_root(resolved):
            raise RecipeError(f"recipe path escapes recipe directory: {path}")
        if not resolved.is_file():
            raise RecipeError(f"recipe not found: {path}")
        try:
            doc = yaml.safe_load(resolved.read_text())
        except yaml.YAMLError as exc:
            raise RecipeError(f"recipe YAML is invalid: {resolved}") from exc
        if not isinstance(doc, dict):
            raise RecipeError(f"recipe document must be a mapping: {resolved}")
        name = resolved.name.removesuffix(_SUFFIX)
        if doc.get("name") != name:
            raise RecipeError(
                f"recipe document name must equal filename {name!r}: {resolved}"
            )
        kind: Literal["manual", "generated"] = (
            "generated" if isinstance(doc.get("x-lockstep-generated"), dict) else "manual"
        )
        return RecipeRef(name=name, path=resolved, kind=kind)

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
        try:
            return self.discover()[str(name_or_path)]
        except KeyError as exc:
            raise RecipeError(f"recipe not found: {name_or_path!r}; runnable recipes end in {_SUFFIX}") from exc

    def load(self, ref: RecipeRef) -> dict[str, Any]:
        verified = self._ref_for_path(ref.path)
        if verified != ref:
            raise RecipeError(f"recipe reference changed while loading: {ref.path}")
        doc = yaml.safe_load(ref.path.read_text())
        assert isinstance(doc, dict)  # _ref_for_path verified this shape
        return doc
