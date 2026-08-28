"""Closed typed model for trusted authoring recovery journals."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from lockstep.authoring_bundle import LeafIdentity, PathIdentity
from lockstep.errors import AuthoringError
from lockstep.recipe.authority import RecipeLimits


_JOURNAL_SCHEMA = "lockstep.authoring-transaction/v1"
MAX_RECOVERY_JOURNAL_BYTES = 16 * 1024 * 1024
_MAX_TEXT_BYTES = 4096
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class RecoveryBeforeImage:
    content: bytes | None
    sha256: str | None
    mode: int | None
    leaf: LeafIdentity | None

    @property
    def absent(self) -> bool:
        return self.content is None


@dataclass(frozen=True, slots=True)
class RecoveryAfterImage:
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True, slots=True)
class RecoveryReadEntry:
    role: str
    path: Path
    sha256: str
    size: int
    leaf: LeafIdentity
    ancestors: tuple[PathIdentity, ...]


@dataclass(frozen=True, slots=True)
class RecoveryWriteEntry:
    index: int
    role: str
    path: Path
    before: RecoveryBeforeImage
    after: RecoveryAfterImage
    ancestors: tuple[PathIdentity, ...]

    def publication_stage(self, operation_id: str) -> Path:
        return self.path.parent / (
            f".{self.path.name}.lockstep-{operation_id}-{self.index}.tmp"
        )

    def restoration_stage(self, operation_id: str) -> Path:
        return self.path.parent / (
            f".{self.path.name}.lockstep-{operation_id}-{self.index}-recovery.tmp"
        )


@dataclass(frozen=True, slots=True)
class AuthoringRecoveryModel:
    operation_id: str
    project: PathIdentity
    read_set: tuple[RecoveryReadEntry, ...]
    write_set: tuple[RecoveryWriteEntry, ...]
    replacement_progress: tuple[int, ...]


def parse_recovery_journal(
    encoded: bytes, *, expected_project: PathIdentity
) -> AuthoringRecoveryModel:
    """Parse owner-state bytes without allowing journal data to mint paths."""

    if not isinstance(encoded, bytes) or len(encoded) > MAX_RECOVERY_JOURNAL_BYTES:
        raise AuthoringError("authoring recovery journal exceeds its byte limit")
    try:
        document = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise AuthoringError("authoring recovery journal is malformed") from exc
    parser = _RecoveryParser(expected_project)
    return parser.parse(document)


class _RecoveryParser:
    __slots__ = ("limits", "project")

    def __init__(self, project: PathIdentity) -> None:
        self.project = project
        self.limits = RecipeLimits()

    def parse(self, value: object) -> AuthoringRecoveryModel:
        document = self._mapping(
            value,
            {
                "schema",
                "operation_id",
                "project",
                "read_set",
                "write_set",
                "replacement_progress",
            },
            "journal",
        )
        if document["schema"] != _JOURNAL_SCHEMA:
            raise AuthoringError("authoring recovery journal schema is unsupported")
        operation_id = self._operation_id(document["operation_id"])
        project = self._project(document["project"])
        read_set = self._read_set(document["read_set"])
        write_set = self._write_set(document["write_set"], read_set)
        progress = self._progress(document["replacement_progress"], len(write_set))
        return AuthoringRecoveryModel(
            operation_id, project, read_set, write_set, progress
        )

    def _project(self, value: object) -> PathIdentity:
        identity = self._path_identity(value, "project identity")
        if identity != self.project:
            raise AuthoringError("authoring recovery journal names another project")
        return identity

    def _read_set(self, value: object) -> tuple[RecoveryReadEntry, ...]:
        values = self._sequence(value, "read set")
        if not values or len(values) > self.limits.max_files:
            raise AuthoringError("authoring recovery read set is outside its bounds")
        entries: list[RecoveryReadEntry] = []
        roles: set[str] = set()
        paths: set[Path] = set()
        aggregate = 0
        for raw in values:
            item = self._mapping(
                raw,
                {"role", "path", "sha256", "size", "leaf", "ancestors"},
                "read entry",
            )
            role = self._text(item["role"], "read role")
            path = self._project_path(item["path"], "read path")
            size = self._size(item["size"], "read size")
            aggregate += size
            leaf = self._leaf(item["leaf"], "read leaf")
            ancestors = self._ancestors(item["ancestors"], path, complete=True)
            if leaf.resolved_path != path or leaf.size != size:
                raise AuthoringError("authoring recovery read leaf is inconsistent")
            if role in roles or path in paths:
                raise AuthoringError("authoring recovery read set contains duplicates")
            roles.add(role)
            paths.add(path)
            entries.append(
                RecoveryReadEntry(
                    role,
                    path,
                    self._digest(item["sha256"], "read digest"),
                    size,
                    leaf,
                    ancestors,
                )
            )
        if aggregate > self.limits.max_source_bytes:
            raise AuthoringError("authoring recovery read set exceeds its byte limit")
        return tuple(entries)

    def _write_set(
        self, value: object, read_set: tuple[RecoveryReadEntry, ...]
    ) -> tuple[RecoveryWriteEntry, ...]:
        values = self._sequence(value, "write set")
        if not values or len(values) > self.limits.max_files:
            raise AuthoringError("authoring recovery write set is outside its bounds")
        roles = {entry.role for entry in read_set}
        paths: set[Path] = set()
        before_bytes = 0
        after_bytes = 0
        entries: list[RecoveryWriteEntry] = []
        for index, raw in enumerate(values):
            item = self._mapping(
                raw,
                {"role", "path", "before", "after", "ancestors"},
                "write entry",
            )
            role = self._text(item["role"], "write role")
            path = self._project_path(item["path"], "write path")
            if role not in roles or path in paths:
                raise AuthoringError("authoring recovery write set is inconsistent")
            paths.add(path)
            before = self._before(item["before"], path)
            after = self._after(item["after"])
            ancestors = self._ancestors(
                item["ancestors"], path, complete=not before.absent
            )
            before_bytes += len(before.content or b"")
            after_bytes += after.size
            entries.append(
                RecoveryWriteEntry(index, role, path, before, after, ancestors)
            )
        if max(before_bytes, after_bytes) > self.limits.max_source_bytes:
            raise AuthoringError("authoring recovery write bytes exceed their limit")
        return tuple(entries)

    def _before(self, value: object, path: Path) -> RecoveryBeforeImage:
        if not isinstance(value, dict) or type(value.get("absent")) is not bool:
            raise AuthoringError("authoring recovery before-image is malformed")
        if value["absent"]:
            self._mapping(value, {"absent"}, "absent before-image")
            return RecoveryBeforeImage(None, None, None, None)
        item = self._mapping(
            value,
            {"absent", "bytes", "sha256", "mode", "leaf"},
            "present before-image",
        )
        content = self._base64(item["bytes"])
        if len(content) > self.limits.max_file_bytes:
            raise AuthoringError("authoring recovery before-image exceeds its limit")
        digest = self._digest(item["sha256"], "before digest")
        if _sha256(content) != digest:
            raise AuthoringError("authoring recovery before digest is inconsistent")
        mode = self._mode(item["mode"], "before mode")
        leaf = self._leaf(item["leaf"], "before leaf")
        if (
            leaf.resolved_path != path
            or leaf.size != len(content)
            or stat.S_IMODE(leaf.mode) != mode
        ):
            raise AuthoringError("authoring recovery before leaf is inconsistent")
        return RecoveryBeforeImage(content, digest, mode, leaf)

    def _after(self, value: object) -> RecoveryAfterImage:
        item = self._mapping(value, {"sha256", "size", "mode"}, "after-image")
        return RecoveryAfterImage(
            self._digest(item["sha256"], "after digest"),
            self._size(item["size"], "after size"),
            self._mode(item["mode"], "after mode"),
        )

    def _ancestors(
        self, value: object, path: Path, *, complete: bool
    ) -> tuple[PathIdentity, ...]:
        values = self._sequence(value, "ancestor chain")
        if not values or len(values) > self.limits.max_depth:
            raise AuthoringError("authoring recovery ancestor chain is invalid")
        ancestors = tuple(
            self._path_identity(item, "ancestor identity") for item in values
        )
        if ancestors[0] != self.project:
            raise AuthoringError("authoring recovery ancestor chain is not rooted")
        previous = self.project.resolved_path.parent
        seen: set[Path] = set()
        for identity in ancestors:
            if (
                identity.resolved_path.parent != previous
                or identity.resolved_path in seen
            ):
                raise AuthoringError(
                    "authoring recovery ancestor chain is not contiguous"
                )
            seen.add(identity.resolved_path)
            previous = identity.resolved_path
        try:
            path.parent.relative_to(previous)
        except ValueError as exc:
            raise AuthoringError(
                "authoring recovery ancestor chain escapes its path"
            ) from exc
        if complete and previous != path.parent:
            raise AuthoringError("authoring recovery ancestor chain is incomplete")
        return ancestors

    def _progress(self, value: object, count: int) -> tuple[int, ...]:
        values = self._sequence(value, "replacement progress")
        if len(values) > count or any(type(item) is not int for item in values):
            raise AuthoringError("authoring recovery replacement progress is invalid")
        progress = tuple(values)
        if progress != tuple(range(len(progress))):
            raise AuthoringError(
                "authoring recovery replacement progress is not monotonic"
            )
        return progress

    def _path_identity(self, value: object, label: str) -> PathIdentity:
        item = self._mapping(value, {"path", "device", "inode"}, label)
        return PathIdentity(
            self._absolute_path(item["path"], label),
            self._counter(item["device"], f"{label} device"),
            self._counter(item["inode"], f"{label} inode"),
        )

    def _leaf(self, value: object, label: str) -> LeafIdentity:
        item = self._mapping(
            value,
            {
                "path",
                "device",
                "inode",
                "mode",
                "size",
                "mtime_ns",
                "ctime_ns",
            },
            label,
        )
        mode = self._counter(item["mode"], f"{label} mode")
        if not stat.S_ISREG(mode):
            raise AuthoringError(f"{label} is not a regular file")
        return LeafIdentity(
            self._absolute_path(item["path"], label),
            self._counter(item["device"], f"{label} device"),
            self._counter(item["inode"], f"{label} inode"),
            mode,
            self._size(item["size"], f"{label} size"),
            self._integer(item["mtime_ns"], f"{label} mtime"),
            self._integer(item["ctime_ns"], f"{label} ctime"),
        )

    def _project_path(self, value: object, label: str) -> Path:
        path = self._absolute_path(value, label)
        try:
            path.relative_to(self.project.resolved_path)
        except ValueError as exc:
            raise AuthoringError(f"{label} is outside the project") from exc
        if path == self.project.resolved_path:
            raise AuthoringError(f"{label} names the project root")
        return path

    @staticmethod
    def _mapping(value: object, keys: set[str], label: str) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != keys:
            raise AuthoringError(f"authoring recovery {label} has an open shape")
        return value

    @staticmethod
    def _sequence(value: object, label: str) -> list[object]:
        if not isinstance(value, list):
            raise AuthoringError(f"authoring recovery {label} must be a list")
        return value

    @staticmethod
    def _text(value: object, label: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
        ):
            raise AuthoringError(f"authoring recovery {label} is invalid")
        return value

    def _absolute_path(self, value: object, label: str) -> Path:
        text = self._text(value, label)
        path = Path(text)
        if (
            not path.is_absolute()
            or Path(os.path.abspath(path)) != path
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise AuthoringError(f"authoring recovery {label} is not canonical")
        return path

    @staticmethod
    def _counter(value: object, label: str) -> int:
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise AuthoringError(f"authoring recovery {label} is invalid")
        return value

    @staticmethod
    def _integer(value: object, label: str) -> int:
        if type(value) is not int or abs(value) > 2**63 - 1:
            raise AuthoringError(f"authoring recovery {label} is invalid")
        return value

    def _size(self, value: object, label: str) -> int:
        size = self._counter(value, label)
        if size > self.limits.max_file_bytes:
            raise AuthoringError(f"authoring recovery {label} exceeds its limit")
        return size

    @staticmethod
    def _mode(value: object, label: str) -> int:
        if type(value) is not int or not 0 <= value <= 0o7777:
            raise AuthoringError(f"authoring recovery {label} is invalid")
        return value

    @staticmethod
    def _digest(value: object, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in _HEX for character in value)
        ):
            raise AuthoringError(f"authoring recovery {label} is invalid")
        return value

    @staticmethod
    def _base64(value: object) -> bytes:
        if not isinstance(value, str):
            raise AuthoringError("authoring recovery before bytes are invalid")
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AuthoringError("authoring recovery before bytes are invalid") from exc

    @staticmethod
    def _operation_id(value: object) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(character not in _HEX for character in value)
        ):
            raise AuthoringError("authoring recovery operation id is invalid")
        return value


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
