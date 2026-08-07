"""RunIndex — runs.json persistence (Task 4).

Persists RunRecord entries, including the FULL brief of the parked step
(review C4), so `scenario_status` never needs to peek into the graph
checkpoint — restart-safe by construction (decision 3). Every mutation
writes `runs.json.tmp` in the state dir then `os.replace`s it into place —
no torn reads visible to a concurrent reader (decision 13: hooks are
read-only on this file).

Statuses: `awaiting | done | escalated | aborted`. `escalated`/`aborted`
are TERMINAL (Global Constraints) — this module doesn't enforce that (the
engine does); it just stores whatever status it's given. `active_only`
means `status == "awaiting"`.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_STATUS = "awaiting"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRecord:
    run_id: str
    recipe: str
    project: str
    status: str
    step: str | None
    brief: dict | None
    started: str
    updated: str


class RunIndex:
    """`runs.json` inside `state_dir`. Atomic tmp+os.replace writes."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / "runs.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.parent / (self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    def create(self, recipe: str, project: str) -> RunRecord:
        run_id = f"{recipe}-{uuid.uuid4().hex[:8]}"
        now = _now()
        record = RunRecord(
            run_id=run_id,
            recipe=recipe,
            project=project,
            status=ACTIVE_STATUS,
            step=None,
            brief=None,
            started=now,
            updated=now,
        )
        data = self._load()
        data[run_id] = asdict(record)
        self._save(data)
        return record

    def get(self, run_id: str) -> RunRecord:
        data = self._load()
        if run_id not in data:
            raise KeyError(run_id)
        return RunRecord(**data[run_id])

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        data = self._load()
        if run_id not in data:
            raise KeyError(run_id)
        data[run_id].update(fields)
        data[run_id]["updated"] = _now()
        self._save(data)
        return RunRecord(**data[run_id])

    def list(self, project: str | None = None, active_only: bool = False) -> list[RunRecord]:
        data = self._load()
        records = [RunRecord(**v) for v in data.values()]
        if project is not None:
            records = [r for r in records if r.project == project]
        if active_only:
            records = [r for r in records if r.status == ACTIVE_STATUS]
        return records

    def db_path(self, run_id: str) -> Path:
        return self._state_dir / "runs" / f"{run_id}.db"
