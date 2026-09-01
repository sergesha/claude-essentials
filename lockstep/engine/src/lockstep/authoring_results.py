"""Pure projections from one captured whole-DAG authoring plan."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import ProjectCompilation
from lockstep.errors import AuthoringError
from lockstep.recipe._authority_models import RecipeCandidate
from lockstep.recipe._recipe_ingress import inspect_recipe_bytes
from lockstep.recipe.profile import CompilerProvenance, _create_compiler_provenance


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    """Canonical proof and authority candidate from one exact observation."""

    proof: CompilerProvenance
    candidate: RecipeCandidate


def _captured_destination_maps(
    compilation: ProjectCompilation,
) -> tuple[dict[Path, bytes | None], dict[Path, bytes]]:
    before = {target.path: target.before for target in compilation.plan.targets}
    after = {target.path: target.after for target in compilation.plan.targets}
    return before, after


def _require_canonical_destinations(
    before: dict[Path, bytes | None], after: dict[Path, bytes]
) -> None:
    for path, expected in after.items():
        observed = before[path]
        if observed is None:
            raise AuthoringError(f"generated canonical file is missing: {path}")
        if observed != expected:
            raise AuthoringError(
                f"generated file is not a byte-for-byte canonical match: {path}"
            )


def _captured_recipe_sources(
    compilation: ProjectCompilation, before: dict[Path, bytes | None]
) -> dict[str, bytes]:
    recipes = compilation.plan.project / ".lockstep" / "recipes"
    captured: dict[str, bytes] = {}
    for path, content in before.items():
        if path.suffixes[-2:] != [".recipe", ".yaml"] or content is None:
            continue
        try:
            relative = path.relative_to(recipes).as_posix()
        except ValueError as exc:
            raise AuthoringError("planned recipe destination escapes recipe root") from exc
        captured[relative] = content
    return captured


def canonical_observation(compilation: ProjectCompilation) -> CanonicalObservation:
    """Prove canonical equality and close ingress without another filesystem pass."""

    before, after = _captured_destination_maps(compilation)
    _require_canonical_destinations(before, after)
    raw_recipes = _captured_recipe_sources(compilation, before)
    root = compilation.root_result.root_relative_path
    try:
        raw_root = raw_recipes[root]
    except KeyError as exc:
        raise AuthoringError(f"generated canonical file is missing: {root}") from exc
    candidate = inspect_recipe_bytes(root, raw_recipes)
    execution = {item.path: item.bytes for item in candidate.files}
    if set(execution) != set(raw_recipes):
        raise AuthoringError("captured recipe DAG does not match the planned closure")
    execution_root = execution.pop(root)
    raw_generated = {path: content for path, content in raw_recipes.items() if path != root}
    proof = _create_compiler_provenance(
        raw_root,
        context="canonical-match",
        root_relative_path=root,
        generated_files=raw_generated,
        execution_recipe_bytes=execution_root,
        execution_generated_files=execution,
        source_bundle_sha256=candidate.source_bundle_sha256,
    )
    return CanonicalObservation(proof, candidate)


def diff_planned_compilation(compilation: ProjectCompilation) -> str:
    """Render every captured destination delta against the same plan."""

    before, after = _captured_destination_maps(compilation)
    chunks: list[str] = []
    for path, expected in after.items():
        observed = before[path] or b""
        if observed == expected:
            continue
        chunks.extend(
            difflib.unified_diff(
                observed.decode("utf-8", errors="replace").splitlines(keepends=True),
                expected.decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile=str(path),
                tofile="canonical",
            )
        )
    return "".join(chunks)
