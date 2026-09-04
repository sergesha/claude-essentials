"""Read-only validation and projection for project publication."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from lockstep.runtime._publication_values import (
    PreparedPublication,
    PublicationConflict,
    PublicationJournalError,
    PublicationPhase,
    PublicationReceipt,
    _canonical,
    _digest,
    _image_from_data,
    _same_image,
)
from lockstep.runtime.artifacts import ArtifactRef
from lockstep.runtime.blobs import BlobRef
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    verify_owner_file,
)
from lockstep.runtime.project_paths import PortableProjectPath


class _ProjectPublicationQueries:
    """Read-only publication projections for the stateful publisher facade."""

    @property
    def project_identity(self) -> str:
        return str(self._project.resolve(strict=True))

    def journal_path(self, handle: PreparedPublication) -> Path:
        self._validate_handle(handle)
        return self._journals / f"{handle.journal_digest}.json"

    def commitment_digest(self, handle: PreparedPublication) -> str:
        journal = self._read_journal(handle)
        request = journal["request"]
        if not isinstance(request, dict):  # closed journal validation is defensive
            raise PublicationJournalError("invalid publication journal request")
        try:
            commitment = {
                "schema": "lockstep.publication-commitment/v1",
                "request_digest": _digest(
                    request["authority_request_digest"],
                    "authority request digest",
                ),
                "grant_digest": _digest(request["grant_digest"], "grant digest"),
                "publication_request_digest": handle.request_digest,
                "journal_digest": handle.journal_digest,
                "publisher_binding_digest": self.binding_digest,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationJournalError(
                "publication journal lacks exact commitment fields"
            ) from exc
        return hashlib.sha256(_canonical(commitment)).hexdigest()

    def prepared_for(
        self, effect_id: str, authority_request_digest: str
    ) -> tuple[PreparedPublication, PublicationPhase] | None:
        """Return only the one project-active journal for an exact ledger claim."""

        active = self._read_active_optional()
        if active is None:
            return None
        raw = self._read_journal_digest(active)
        request = raw.get("request")
        if not isinstance(request, dict):
            raise PublicationJournalError("invalid publication journal request")
        if (
            request.get("effect_id") != effect_id
            or request.get("authority_request_digest")
            != authority_request_digest
        ):
            return None
        request_digest = _digest(raw.get("request_digest"), "request digest")
        handle = PreparedPublication(active, request_digest, self.binding_digest)
        journal = self._read_journal(handle)
        return handle, PublicationPhase(str(journal["phase"]))

    def _verify_complete(self, raw_plan: object, *, direction: str) -> None:
        plan = self._validate_plan(raw_plan)
        root_fd = self._open_root()
        try:
            for item in plan:
                parent_fd, leaf, _ancestors = self._open_parent(
                    root_fd,
                    item["destination"],  # type: ignore[arg-type]
                    expected=item["ancestors"],
                )
                try:
                    desired = item["after"] if direction == "apply" else item["before"]
                    if not _same_image(
                        self._current_image(parent_fd, leaf),
                        desired,  # type: ignore[arg-type]
                    ):
                        raise PublicationConflict(
                            f"publication destination changed: {item['destination']}"
                        )
                finally:
                    os.close(parent_fd)
        finally:
            os.close(root_fd)

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._project, flags)
            info = os.fstat(fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_dev != self._root_device
                or info.st_ino != self._root_inode
            ):
                raise PublicationConflict("project root is not a directory")
            return fd
        except OSError as exc:
            raise PublicationConflict("project root cannot be opened safely") from exc

    def _open_parent(
        self,
        root_fd: int,
        destination: str,
        *,
        expected: object | None = None,
    ) -> tuple[int, str, list[dict[str, object]]]:
        path = PurePosixPath(destination)
        current = os.dup(root_fd)
        ancestors: list[dict[str, object]] = []
        try:
            for index, part in enumerate(path.parts[:-1]):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                next_fd = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = next_fd
                info = os.fstat(current)
                ancestors.append(
                    {
                        "path": "/".join(path.parts[: index + 1]),
                        "device": info.st_dev,
                        "inode": info.st_ino,
                    }
                )
            if expected is not None and ancestors != expected:
                raise PublicationConflict(
                    f"publication ancestor changed: {destination}"
                )
            return current, path.name, ancestors
        except OSError as exc:
            os.close(current)
            raise PublicationConflict(
                f"publication parent is missing or unsafe: {destination}"
            ) from exc

    def _current_image_data(
        self, parent_fd: int, leaf: str
    ) -> tuple[BlobRef, int, bytes] | None:
        try:
            info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise PublicationConflict("publication destination is not a regular file")
        if info.st_size > self._limits.max_file_bytes:
            raise StorageLimitExceeded("publication destination exceeds admission limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise PublicationConflict("publication destination cannot be read safely") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > self._limits.max_file_bytes:
                raise PublicationConflict("publication destination changed during read")
            chunks: list[bytes] = []
            remaining = self._limits.max_file_bytes + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > self._limits.max_file_bytes:
                raise StorageLimitExceeded("publication destination exceeds admission limit")
        finally:
            os.close(fd)
        return (
            BlobRef(hashlib.sha256(data).hexdigest(), len(data)),
            stat.S_IMODE(opened.st_mode),
            data,
        )

    def _current_image(
        self, parent_fd: int, leaf: str
    ) -> tuple[BlobRef, int] | None:
        observed = self._current_image_data(parent_fd, leaf)
        if observed is None:
            return None
        return observed[0], observed[1]

    def _read_active_optional(self) -> str | None:
        if not self._active.exists() and not self._active.is_symlink():
            return None
        data = self._read_json(self._active)
        if (
            set(data) != {"schema", "journal_digest"}
            or data["schema"] != "lockstep.active-publication/v1"
        ):
            raise PublicationJournalError("invalid active publication pointer")
        try:
            return _digest(data["journal_digest"], "journal digest")
        except ValueError as exc:
            raise PublicationJournalError("invalid active publication pointer") from exc

    def _read_journal_digest(self, digest: str) -> dict[str, object]:
        handle = PreparedPublication(digest, "0" * 64, self.binding_digest)
        data = self._read_json(self.journal_path(handle))
        if (
            set(data) != {
                "schema", "phase", "request_digest", "publisher_binding_digest",
                "request", "plan", "cursor",
            }
            or data.get("schema") != "lockstep.publication-journal/v1"
            or data.get("phase") not in {phase.value for phase in PublicationPhase}
        ):
            raise PublicationJournalError("invalid publication journal")
        return data

    def _read_journal(self, handle: PreparedPublication) -> dict[str, object]:
        data = self._read_json(self.journal_path(handle))
        try:
            if set(data) != {
                "schema", "phase", "request_digest", "publisher_binding_digest",
                "request", "plan", "cursor",
            }:
                raise ValueError
            if data["schema"] != "lockstep.publication-journal/v1":
                raise ValueError
            if data["phase"] not in {phase.value for phase in PublicationPhase}:
                raise ValueError
            if data["request_digest"] != handle.request_digest:
                raise ValueError
            if data["publisher_binding_digest"] != self.binding_digest:
                raise ValueError
            expected_key = hashlib.sha256(
                _canonical(
                    {
                        "schema": "lockstep.publication-journal-key/v1",
                        "request_digest": handle.request_digest,
                        "publisher_binding_digest": self.binding_digest,
                    }
                )
            ).hexdigest()
            if expected_key != handle.journal_digest:
                raise ValueError
            request = data["request"]
            if not isinstance(request, dict):
                raise ValueError
            if hashlib.sha256(_canonical(request)).hexdigest() != handle.request_digest:
                raise ValueError
            plan = self._validate_plan(data["plan"])
            cursor = data["cursor"]
            if type(cursor) is not int or cursor < -1 or cursor > len(plan):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationJournalError("invalid publication journal") from exc
        return data

    def _read_json(self, path: Path) -> dict[str, object]:
        try:
            verify_owner_file(path)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > self._limits.max_journal_bytes:
                    raise PublicationJournalError("publication journal is not bounded")
                encoded = os.read(fd, self._limits.max_journal_bytes + 1)
            finally:
                os.close(fd)
            value = json.loads(encoded)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except PublicationJournalError:
            raise
        except (OSError, InsecureStatePath, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PublicationJournalError(f"cannot read publication journal: {path}") from exc

    def _validate_plan(self, value: object) -> list[dict[str, object]]:
        if not isinstance(value, list) or not value or len(value) > self._limits.max_entries:
            raise PublicationJournalError("invalid publication plan")
        checked: list[dict[str, object]] = []
        total_bytes = 0
        for item in value:
            if not isinstance(item, dict) or set(item) != {
                "artifact_ref", "destination", "transformation", "before", "after",
                "ancestors",
            }:
                raise PublicationJournalError("invalid publication plan entry")
            try:
                ArtifactRef.parse(item["artifact_ref"])
                PortableProjectPath.parse(item["destination"], "file")
                if item["transformation"] != "identity":
                    raise ValueError
                before = _image_from_data(item["before"])
                after = _image_from_data(item["after"])
                if after is None:
                    raise ValueError
                total_bytes += after[0].size
                if before is not None:
                    total_bytes += before[0].size
                if total_bytes > self._limits.max_total_bytes:
                    raise StorageLimitExceeded(
                        "publication aggregate bytes exceed admission limit"
                    )
                raw_ancestors = item["ancestors"]
                if not isinstance(raw_ancestors, list):
                    raise ValueError
                ancestors: list[dict[str, object]] = []
                for ancestor in raw_ancestors:
                    if not isinstance(ancestor, dict) or set(ancestor) != {
                        "path", "device", "inode"
                    }:
                        raise ValueError
                    path = PortableProjectPath.parse(ancestor["path"], "directory")
                    if (
                        type(ancestor["device"]) is not int
                        or ancestor["device"] < 0
                        or type(ancestor["inode"]) is not int
                        or ancestor["inode"] < 0
                    ):
                        raise ValueError
                    ancestors.append(
                        {
                            "path": path.value,
                            "device": ancestor["device"],
                            "inode": ancestor["inode"],
                        }
                    )
            except (TypeError, ValueError) as exc:
                raise PublicationJournalError("invalid publication plan entry") from exc
            checked.append(
                {
                    **item,
                    "before": before,
                    "after": after,
                    "ancestors": ancestors,
                }
            )
        return checked

    def _validate_handle(self, handle: PreparedPublication) -> None:
        if not isinstance(handle, PreparedPublication):
            raise TypeError("publication handle must be a closed value")
        _digest(handle.journal_digest, "journal digest")
        _digest(handle.request_digest, "request digest")
        if handle.publisher_binding_digest != self.binding_digest:
            raise PublicationConflict("publication handle names another publisher")

    def _receipt(
        self, handle: PreparedPublication, phase: PublicationPhase
    ) -> PublicationReceipt:
        return PublicationReceipt(
            handle.journal_digest, handle.request_digest, phase.value
        )
