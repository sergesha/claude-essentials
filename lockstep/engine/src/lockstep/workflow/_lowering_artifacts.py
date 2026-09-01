"""Contract-bound artifact producer grouping."""

from __future__ import annotations

from typing import Any, Mapping  # noqa: UP035 - preserves existing hints

from ._lowering_artifact_matching import artifact_producer_candidates


class _LoweringArtifacts:
    @staticmethod
    def _artifact_producers(
        resolved: Any,
        artifact_specs: Mapping[str, tuple[str, str, str, str, str]],
    ) -> dict[str, tuple[tuple[str, str, str, str, str, str], ...]]:
        by_file: dict[str, list[tuple[str, str, str, str, str, str]]] = {}
        for qualified, spec in artifact_specs.items():
            candidates = artifact_producer_candidates(resolved, qualified, spec)
            if len(candidates) != 1:
                source = spec[1]
                raise ValueError(
                    f"child artifact source {source!r} requires exactly one "
                    "contract-bound producer"
                )
            relative_path, candidate = candidates[0]
            by_file.setdefault(relative_path, []).append(candidate)
        return {key: tuple(value) for key, value in by_file.items()}
