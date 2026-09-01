from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from lockstep.runtime.manifests import snapshot_from_data, snapshot_to_data
from lockstep.runtime.manifests import ProjectSnapshot as FilesystemSnapshot
from lockstep.runtime.owner_state import InsecureStatePath, seal_owner_file, verify_owner_file
from lockstep.runtime.providers._workspace_core import (
    WorkspaceContext,
    WorkspaceError,
    WorkspaceLease,
    _canonical,
    _hex,
    _snapshot_ref,
    _text,
    _workspace_digest,
)

class WorkspaceRecordRepository:
    def __init__(self, context: WorkspaceContext) -> None:
        self._context = context

    def inspect(self, workspace_ref: str): return self._read_record(_workspace_digest(workspace_ref))
    def _record_path(self, key: str) -> Path:
        return self._context.records / f"{key}.json"

    def _checkout_path(self, key: str) -> Path:
        return self._context.checkouts / key

    def _staging_path(self, key: str) -> Path:
        return self._context.staging / key

    def _quarantine_path(self, key: str) -> Path:
        return self._context.quarantine / key

    def _discard_recovery_tree(self, path: Path, parent: Path) -> None:
        if path.parent != parent or path.is_symlink():
            raise WorkspaceError("workspace recovery target failed containment check")
        if path.exists():
            if not path.is_dir():
                raise WorkspaceError("workspace recovery target is not a directory")
            shutil.rmtree(path)
            self._fsync_directory(parent)

    def _write_record(
        self, lease: WorkspaceLease, *, snapshot_ref_out: str | None
    ) -> None:
        key = _workspace_digest(lease.workspace_ref)
        data = {
            "schema": "lockstep.workspace-lease/v1",
            "workspace_ref": lease.workspace_ref,
            "effect_id": lease.effect_id,
            "request_digest": lease.request_digest,
            "input_snapshot_ref": lease.input_snapshot_ref,
            "revision": lease.revision,
            "declared_writes": list(lease.declared_writes),
            "purpose": lease.purpose,
            "baseline": snapshot_to_data(lease.baseline),
            "vcs_baseline_digest": lease.vcs_baseline_digest,
            "phase": lease.phase,
            "snapshot_ref": snapshot_ref_out,
        }
        encoded = _canonical(data)
        path = self._record_path(key)
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{key}.", dir=self._context.records
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(temporary, writable=False)
            os.replace(temporary, path)
            self._fsync_directory(self._context.records)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _read_record(self, key: str) -> WorkspaceLease:
        path = self._record_path(key)
        try:
            verify_owner_file(path)
            data = json.loads(path.read_bytes())
            if (
                not isinstance(data, dict)
                or data.get("schema") != "lockstep.workspace-lease/v1"
            ):
                raise WorkspaceError("invalid workspace lease record")
            workspace_ref = data["workspace_ref"]
            if _workspace_digest(workspace_ref) != key:
                raise WorkspaceError("workspace lease address mismatch")
            revision = data["revision"]
            if type(revision) is not int or revision <= 0:
                raise WorkspaceError("workspace revision must be a positive integer")
            phase = data["phase"]
            if phase not in {"materialized", "quarantined", "released"}:
                raise WorkspaceError("invalid workspace phase")
            purpose = data.get("purpose", "managed_output")
            if purpose not in {"managed_output", "no_publish_operation"}:
                raise WorkspaceError("invalid workspace purpose")
            declared = data["declared_writes"]
            if not isinstance(declared, list) or not all(
                isinstance(item, str) for item in declared
            ):
                raise WorkspaceError("invalid workspace declared writes")
            expected_path = (
                self._checkout_path(key)
                if phase == "materialized"
                else self._quarantine_path(key)
            )
            return WorkspaceLease(
                workspace_ref=workspace_ref,
                effect_id=_text(data["effect_id"], "effect_id"),
                request_digest=_hex(data["request_digest"], "request digest"),
                input_snapshot_ref=(
                    f"snapshot:{_snapshot_ref(data['input_snapshot_ref']).digest}"
                ),
                revision=revision,
                workspace_path=expected_path,
                declared_writes=tuple(declared),
                purpose=purpose,
                baseline=snapshot_from_data(data["baseline"]),
                vcs_baseline_digest=_hex(
                    data["vcs_baseline_digest"], "VCS baseline digest"
                ),
                phase=phase,
                rollover_snapshot_ref=(
                    None
                    if data.get("snapshot_ref") is None
                    else f"snapshot:{_snapshot_ref(data['snapshot_ref']).digest}"
                ),
            )
        except FileNotFoundError as exc:
            raise KeyError(f"workspace:{key}") from exc
        except (
            InsecureStatePath,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise WorkspaceError("invalid or insecure workspace lease record") from exc

    def _stored_rollover_ref(self, key: str) -> str | None:
        path = self._record_path(key)
        try:
            verify_owner_file(path)
            data = json.loads(path.read_bytes())
            value = data.get("snapshot_ref")
        except (OSError, ValueError, TypeError, InsecureStatePath) as exc:
            raise WorkspaceError("cannot read workspace rollover state") from exc
        if value is None:
            return None
        return f"snapshot:{_snapshot_ref(value).digest}"

    def _validate_current_lease(
        self, supplied: WorkspaceLease, current: WorkspaceLease
    ) -> None:
        if not isinstance(supplied, WorkspaceLease):
            raise WorkspaceError("typed workspace lease is required")
        if supplied != current:
            raise WorkspaceError("workspace lease revision is stale")

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
