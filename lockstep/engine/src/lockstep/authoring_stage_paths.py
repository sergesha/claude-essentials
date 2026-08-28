"""Deterministic reserved paths and durable absence evidence for authoring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReservedStagePaths:
    index: int
    publication: Path
    restoration: Path


@dataclass(frozen=True, slots=True)
class ReservedStageEvidence:
    operation_id: str
    stages: tuple[ReservedStagePaths, ...]


def reserved_stage_paths(
    destination: Path, operation_id: str, index: int
) -> ReservedStagePaths:
    stem = f".{destination.name}.lockstep-{operation_id}-{index}"
    return ReservedStagePaths(
        index,
        destination.parent / f"{stem}.tmp",
        destination.parent / f"{stem}-recovery.tmp",
    )


def reserved_stage_set(
    destinations: tuple[Path, ...], operation_id: str
) -> tuple[ReservedStagePaths, ...]:
    return tuple(
        reserved_stage_paths(destination, operation_id, index)
        for index, destination in enumerate(destinations)
    )
