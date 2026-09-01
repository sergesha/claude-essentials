"""Fixed-population enumeration for run-drive recovery."""

from __future__ import annotations

from collections.abc import Iterator

from lockstep.runtime.effects.ledger import RunDriveWatch


class _RecoveryWatchEnumeration:
    def _sweep_run_drive_watches(
        self,
        *,
        project_identity: str | None,
        limit: int,
    ) -> tuple[str, ...]:
        high_water = self._effects.max_run_drive_admission_seq()
        inserted_public_run_ids = self._backfill.apply_next_page()
        if limit < 1:
            return ()
        recovered: list[str] = []
        if high_water is not None:
            for watches in self._watch_pages(
                high_water=high_water, page_size=128
            ):
                recovered.extend(
                    self._accepted_from(
                        watches,
                        project_identity=project_identity,
                        limit=limit - len(recovered),
                    )
                )
                if len(recovered) == limit:
                    return tuple(recovered)
        if inserted_public_run_ids:
            watches = self._effects.list_run_drive_watches_by_public_run_ids(
                inserted_public_run_ids
            )
            recovered.extend(
                self._accepted_from(
                    watches,
                    project_identity=project_identity,
                    limit=limit - len(recovered),
                )
            )
        return tuple(recovered)

    def _watch_pages(
        self, *, high_water: int, page_size: int
    ) -> Iterator[tuple[RunDriveWatch, ...]]:
        cursor = 0
        while cursor < high_water:
            watches = self._effects.list_run_drive_watches(
                after_admission_seq=cursor,
                high_water=high_water,
                limit=page_size,
            )
            if not watches:
                return
            yield watches
            cursor = watches[-1].admission_seq
