"""One exact resource contract for authoring plans and publication."""

from __future__ import annotations

from collections.abc import Iterable

from lockstep.recipe.authority import RecipeLimits
from lockstep.runtime.owner_state import StorageLimitExceeded


class AuthoringBudget:
    """Incrementally admit one bounded authoring record group."""

    __slots__ = ("_bytes", "_count", "_label", "_limits")

    def __init__(self, label: str) -> None:
        self._label = label
        self._limits = RecipeLimits()
        self._count = 0
        self._bytes = 0

    @property
    def max_bytes_for_next(self) -> int:
        if self._count >= self._limits.max_files:
            raise StorageLimitExceeded(
                f"{self._label} exceeds {self._limits.max_files} admission limit"
            )
        return min(
            self._limits.max_file_bytes,
            self._limits.max_source_bytes - self._bytes,
        )

    def retain(self, content: bytes | None) -> None:
        available = self.max_bytes_for_next
        if content is not None and not isinstance(content, bytes):
            raise TypeError("authoring budget contents must be bytes or absence")
        size = len(content or b"")
        if size > available:
            if size > self._limits.max_file_bytes:
                raise StorageLimitExceeded(
                    f"{self._label} contains a file exceeding the admission limit"
                )
            raise StorageLimitExceeded(
                f"{self._label} exceeds the aggregate byte admission limit"
            )
        self._count += 1
        self._bytes += size


def validate_authoring_contents(
    label: str, contents: Iterable[bytes | None]
) -> None:
    budget = AuthoringBudget(label)
    for content in contents:
        budget.retain(content)
