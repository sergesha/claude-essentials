"""Immutable artifact provenance over Task 2 blobs and project snapshots.

Artifact bytes are never copied into a second staging store.  Admission selects
an exact file from a trusted, immutable managed-workspace rollover snapshot and
publishes a write-once provenance manifest.  The producer key is the native
effect coordinate plus the descriptor-declared name; a retry at a new native
coordinate is therefore a distinct producer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lockstep.runtime.blobs import BlobRef, BlobStore, DigestMismatch
from lockstep.runtime.locking import file_lock
from lockstep.runtime.native_models import NativeCoordinate
from lockstep.runtime.owner_state import (
    InsecureStatePath,
    StorageLimitExceeded,
    ensure_owner_directory,
    fsync_owner_directory,
    initialize_owner_state,
    seal_owner_file,
    take_bounded,
    verify_owner_file,
)
from lockstep.runtime.project_paths import PortableProjectPath
from lockstep.runtime.project_snapshots import ProjectSnapshotRef, ProjectSnapshotStore


_HEX = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$"
)


class ArtifactError(RuntimeError):
    pass


class ArtifactCollision(ArtifactError):
    pass


class ArtifactProvenanceError(ArtifactError):
    pass


class MissingArtifact(ArtifactError):
    pass


@dataclass(frozen=True, order=True)
class ArtifactRef:
    digest: str

    def __post_init__(self) -> None:
        _digest(self.digest, "artifact reference")

    def __str__(self) -> str:
        return f"artifact:{self.digest}"

    @classmethod
    def parse(cls, value: ArtifactRef | str) -> ArtifactRef:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or not value.startswith("artifact:"):
            raise ValueError("artifact reference must use artifact:<sha256>")
        return cls(value.removeprefix("artifact:"))


@dataclass(frozen=True)
class ArtifactDeclaration:
    name: str
    source_path: str
    media_type: str
    required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("artifact name must be a bounded logical name")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(
            self.media_type
        ):
            raise ValueError("artifact media type is invalid")
        if type(self.required) is not bool:
            raise TypeError("artifact required must be a boolean")
        try:
            portable = PortableProjectPath.parse(self.source_path, "file")
        except ValueError as exc:
            raise ValueError("artifact source must be a safe project file") from exc
        if ".git" in portable.relative.parts:
            raise ValueError("artifact source may not name Git controls")
        object.__setattr__(self, "source_path", portable.value)


@dataclass(frozen=True)
class ArtifactRecord:
    ref: ArtifactRef
    public_run_id: str
    project_identity: str
    definition_digest: str
    producer_effect_id: str
    producer_request_digest: str
    workspace_ref: str
    producer_coordinate: NativeCoordinate
    descriptor_digest: str
    declared_name: str
    source_path: str
    media_type: str
    blob: BlobRef
    source_snapshot_ref: ProjectSnapshotRef
    producer_set_digest: str


@dataclass(frozen=True)
class ArtifactLimits:
    max_artifacts_per_set: int = 32
    max_manifest_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_artifacts_per_set <= 0 or self.max_manifest_bytes <= 0:
            raise ValueError("artifact limits must be positive")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _coordinate_data(value: NativeCoordinate) -> dict[str, str]:
    if not isinstance(value, NativeCoordinate):
        raise TypeError("producer coordinate must be NativeCoordinate")
    fields = {
        "thread_id": value.thread_id,
        "checkpoint_ns": value.checkpoint_ns,
        "checkpoint_id": value.checkpoint_id,
        "task_id": value.task_id,
        "interrupt_id": value.interrupt_id,
    }
    checked: dict[str, str] = {}
    for key, item in fields.items():
        if key == "checkpoint_ns":
            if not isinstance(item, str) or len(item.encode("utf-8")) > 4096:
                raise ValueError("coordinate checkpoint_ns must be bounded text")
            checked[key] = item
        else:
            checked[key] = _text(item, f"coordinate {key}")
    return checked


def _coordinate_from_data(value: object) -> NativeCoordinate:
    fields = {"thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "interrupt_id"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid artifact producer coordinate")
    checked: dict[str, str] = {}
    for key in fields:
        item = value[key]
        if key == "checkpoint_ns":
            if not isinstance(item, str) or len(item.encode("utf-8")) > 4096:
                raise ValueError("coordinate checkpoint_ns must be bounded text")
            checked[key] = item
        else:
            checked[key] = _text(item, f"coordinate {key}")
    return NativeCoordinate(
        thread_id=checked["thread_id"],
        checkpoint_ns=checked["checkpoint_ns"],
        checkpoint_id=checked["checkpoint_id"],
        task_id=checked["task_id"],
        interrupt_id=checked["interrupt_id"],
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _read_regular(path: Path, *, max_bytes: int) -> bytes:
    try:
        verify_owner_file(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        raise
    except (OSError, InsecureStatePath) as exc:
        raise ArtifactError(f"unsafe artifact registry path: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ArtifactError("artifact manifest is not a bounded regular file")
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise StorageLimitExceeded("artifact manifest exceeds admission limit")
        return data
    finally:
        os.close(fd)


class ArtifactRegistry:
    def __init__(
        self,
        owner_state_dir: str | Path,
        blob_store: BlobStore,
        snapshot_store: ProjectSnapshotStore,
        *,
        limits: ArtifactLimits | None = None,
    ) -> None:
        self._owner_state = initialize_owner_state(owner_state_dir)
        self._manifests = ensure_owner_directory(self._owner_state, "artifacts/manifests")
        self._keys = ensure_owner_directory(self._owner_state, "artifacts/producer-keys")
        self._sets = ensure_owner_directory(self._owner_state, "artifacts/producer-sets")
        self._blobs = blob_store
        self._snapshots = snapshot_store
        self._limits = limits or ArtifactLimits()

    def _manifest_path(self, ref: ArtifactRef) -> Path:
        return self._manifests / f"{ref.digest}.json"

    def _key_digest(
        self,
        effect_id: str,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        name: str,
    ) -> str:
        encoded = _canonical(
            {
                "schema": "lockstep.artifact-producer-key/v1",
                "effect_id": _text(effect_id, "producer effect_id"),
                "coordinate": _coordinate_data(coordinate),
                "descriptor_digest": _digest(
                    descriptor_digest, "descriptor digest"
                ),
                "name": _text(name, "declared artifact name"),
            }
        )
        return hashlib.sha256(encoded).hexdigest()

    def _key_path(
        self,
        effect_id: str,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        name: str,
    ) -> Path:
        digest = self._key_digest(effect_id, coordinate, descriptor_digest, name)
        return self._keys / f"{digest}.json"

    def _set_digest(
        self,
        effect_id: str,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
    ) -> str:
        return hashlib.sha256(
            _canonical(
                {
                    "schema": "lockstep.artifact-producer-set/v1",
                    "effect_id": _text(effect_id, "producer effect_id"),
                    "coordinate": _coordinate_data(coordinate),
                    "descriptor_digest": _digest(
                        descriptor_digest, "descriptor digest"
                    ),
                }
            )
        ).hexdigest()

    def _set_path(self, digest: str) -> Path:
        return self._sets / f"{_digest(digest, 'producer set digest')}.json"

    def _preflight_immutable(
        self, path: Path, encoded: bytes, *, collision: str
    ) -> None:
        if len(encoded) > self._limits.max_manifest_bytes:
            raise StorageLimitExceeded("artifact manifest exceeds admission limit")
        if path.exists() or path.is_symlink():
            existing = _read_regular(path, max_bytes=self._limits.max_manifest_bytes)
            if existing != encoded:
                raise ArtifactCollision(collision)

    def _publish_immutable(self, path: Path, encoded: bytes, *, collision: str) -> None:
        if len(encoded) > self._limits.max_manifest_bytes:
            raise StorageLimitExceeded("artifact manifest exceeds admission limit")
        with file_lock(path, timeout=30.0, stale_after=300.0):
            self._publish_immutable_locked(path, encoded, collision=collision)

    def _publish_immutable_locked(
        self, path: Path, encoded: bytes, *, collision: str
    ) -> None:
        if path.exists() or path.is_symlink():
            existing = _read_regular(path, max_bytes=self._limits.max_manifest_bytes)
            if existing != encoded:
                raise ArtifactCollision(collision)
            return
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw_tmp)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            seal_owner_file(tmp, writable=False)
            os.replace(tmp, path)
            fsync_owner_directory(path.parent)
        finally:
            if tmp.exists():
                tmp.unlink()

    def register_set(
        self,
        *,
        public_run_id: str,
        project_identity: str,
        definition_digest: str,
        producer_effect_id: str,
        producer_request_digest: str,
        workspace_ref: str,
        producer_coordinate: NativeCoordinate,
        descriptor_digest: str,
        snapshot_ref: ProjectSnapshotRef,
        declarations: Iterable[ArtifactDeclaration],
    ) -> tuple[ArtifactRef, ...]:
        values = take_bounded(
            declarations,
            self._limits.max_artifacts_per_set,
            "artifact declarations",
        )
        if any(not isinstance(item, ArtifactDeclaration) for item in values):
            raise TypeError("artifact declarations must be closed values")
        if len({item.name for item in values}) != len(values):
            raise ArtifactCollision("artifact declarations contain duplicate names")
        set_digest = self._set_digest(
            producer_effect_id, producer_coordinate, descriptor_digest
        )
        set_lock = self._set_path(set_digest)
        with file_lock(set_lock, timeout=30.0, stale_after=300.0):
            return self._register_set_locked(
                public_run_id=public_run_id,
                project_identity=project_identity,
                definition_digest=definition_digest,
                producer_effect_id=producer_effect_id,
                producer_request_digest=producer_request_digest,
                workspace_ref=workspace_ref,
                producer_coordinate=producer_coordinate,
                descriptor_digest=descriptor_digest,
                snapshot_ref=snapshot_ref,
                values=values,
                set_digest=set_digest,
            )

    def _register_set_locked(
        self,
        *,
        public_run_id: str,
        project_identity: str,
        definition_digest: str,
        producer_effect_id: str,
        producer_request_digest: str,
        workspace_ref: str,
        producer_coordinate: NativeCoordinate,
        descriptor_digest: str,
        snapshot_ref: ProjectSnapshotRef,
        values: tuple[ArtifactDeclaration, ...],
        set_digest: str,
    ) -> tuple[ArtifactRef, ...]:
        snapshot = self._snapshots.read(snapshot_ref)
        if snapshot.provenance.get("source") != "managed-workspace-rollover":
            raise ArtifactProvenanceError(
                "artifacts require a managed-workspace rollover snapshot"
            )
        if (
            snapshot.provenance.get("request_digest") != producer_request_digest
            or snapshot.provenance.get("workspace_ref") != workspace_ref
        ):
            raise ArtifactProvenanceError(
                "artifact snapshot does not match the producer request/workspace"
            )
        by_path = {item.path: item.blob for item in snapshot.files}
        prepared: list[tuple[ArtifactRef, bytes, Path, bytes]] = []
        committed_items: list[dict[str, str]] = []
        for declaration in values:
            blob = by_path.get(declaration.source_path)
            if blob is None:
                if declaration.required:
                    raise MissingArtifact(
                        f"required artifact source is absent: {declaration.source_path}"
                    )
                continue
            self._blobs.read(blob)
            data = {
                "schema": "lockstep.artifact/v1",
                "public_run_id": _text(public_run_id, "public_run_id"),
                "project_identity": _text(project_identity, "project_identity"),
                "definition_digest": _digest(definition_digest, "definition digest"),
                "producer_effect_id": _text(producer_effect_id, "producer effect_id"),
                "producer_request_digest": _digest(
                    producer_request_digest, "producer request digest"
                ),
                "workspace_ref": _text(workspace_ref, "workspace_ref"),
                "producer_coordinate": _coordinate_data(producer_coordinate),
                "descriptor_digest": _digest(descriptor_digest, "descriptor digest"),
                "declared_name": declaration.name,
                "source_path": declaration.source_path,
                "media_type": declaration.media_type,
                "blob": {"sha256": blob.sha256, "size": blob.size},
                "source_snapshot_ref": snapshot_ref.digest,
                "producer_set_digest": set_digest,
            }
            encoded = _canonical(data)
            ref = ArtifactRef(hashlib.sha256(encoded).hexdigest())
            key_data = _canonical(
                {"schema": "lockstep.artifact-key/v1", "artifact_ref": str(ref)}
            )
            prepared.append(
                (
                    ref,
                    encoded,
                    self._key_path(
                        producer_effect_id,
                        producer_coordinate,
                        descriptor_digest,
                        declaration.name,
                    ),
                    key_data,
                )
            )
            committed_items.append(
                {"name": declaration.name, "artifact_ref": str(ref)}
            )
        set_data = _canonical(
            {
                "schema": "lockstep.artifact-producer-set-commit/v1",
                "effect_id": producer_effect_id,
                "coordinate": _coordinate_data(producer_coordinate),
                "descriptor_digest": descriptor_digest,
                "artifacts": committed_items,
            }
        )
        # The complete set, including every existing producer-key collision, is
        # checked before the first immutable name is published.
        for ref, encoded, key_path, key_data in prepared:
            self._preflight_immutable(
                key_path,
                key_data,
                collision="artifact producer/name key is already bound differently",
            )
            self._preflight_immutable(
                self._manifest_path(ref),
                encoded,
                collision="artifact manifest digest collision",
            )
        self._preflight_immutable(
            self._set_path(set_digest),
            set_data,
            collision="artifact producer set is already bound differently",
        )
        for ref, encoded, key_path, key_data in prepared:
            self._publish_immutable(
                key_path,
                key_data,
                collision="artifact producer/name key is already bound differently",
            )
            self._publish_immutable(
                self._manifest_path(ref),
                encoded,
                collision="artifact manifest digest collision",
            )
        # This marker is the only visibility boundary. A crash before it may
        # leave immutable blobs/keys, but they remain unreachable garbage.
        self._publish_immutable_locked(
            self._set_path(set_digest),
            set_data,
            collision="artifact producer set is already bound differently",
        )
        return tuple(item[0] for item in prepared)

    def read(self, ref: ArtifactRef | str) -> ArtifactRecord:
        parsed_ref = ArtifactRef.parse(ref)
        path = self._manifest_path(parsed_ref)
        try:
            encoded = _read_regular(path, max_bytes=self._limits.max_manifest_bytes)
        except FileNotFoundError as exc:
            raise KeyError(str(parsed_ref)) from exc
        if hashlib.sha256(encoded).hexdigest() != parsed_ref.digest:
            raise DigestMismatch("artifact manifest differs from its immutable reference")
        try:
            data = json.loads(encoded)
            fields = {
                "schema", "public_run_id", "project_identity", "definition_digest",
                "producer_effect_id", "producer_request_digest", "workspace_ref",
                "producer_coordinate", "descriptor_digest", "declared_name",
                "source_path", "media_type", "blob", "source_snapshot_ref",
                "producer_set_digest",
            }
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError
            if data["schema"] != "lockstep.artifact/v1":
                raise ValueError
            declaration = ArtifactDeclaration(
                data["declared_name"], data["source_path"], data["media_type"], True
            )
            blob_data = data["blob"]
            if not isinstance(blob_data, dict) or set(blob_data) != {"sha256", "size"}:
                raise ValueError
            if type(blob_data["size"]) is not int or blob_data["size"] < 0:
                raise ValueError
            blob = BlobRef(
                _digest(blob_data["sha256"], "blob digest"), blob_data["size"]
            )
            snapshot_ref = ProjectSnapshotRef(
                _digest(data["source_snapshot_ref"], "snapshot reference")
            )
            coordinate = _coordinate_from_data(data["producer_coordinate"])
            record = ArtifactRecord(
                parsed_ref,
                _text(data["public_run_id"], "public_run_id"),
                _text(data["project_identity"], "project_identity"),
                _digest(data["definition_digest"], "definition digest"),
                _text(data["producer_effect_id"], "producer effect_id"),
                _digest(data["producer_request_digest"], "producer request digest"),
                _text(data["workspace_ref"], "workspace_ref"),
                coordinate,
                _digest(data["descriptor_digest"], "descriptor digest"),
                declaration.name,
                declaration.source_path,
                declaration.media_type,
                blob,
                snapshot_ref,
                _digest(data["producer_set_digest"], "producer set digest"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactError("invalid immutable artifact manifest") from exc
        self._snapshots.read(record.source_snapshot_ref)
        self._blobs.read(record.blob)
        committed = self._read_committed_set(record.producer_set_digest)
        if not any(
            item["name"] == record.declared_name
            and item["artifact_ref"] == str(record.ref)
            for item in committed["artifacts"]
        ):
            raise ArtifactProvenanceError(
                "artifact is absent from its committed producer set"
            )
        return record

    def _read_committed_set(self, digest: str) -> dict[str, object]:
        try:
            encoded = _read_regular(
                self._set_path(digest), max_bytes=self._limits.max_manifest_bytes
            )
        except FileNotFoundError as exc:
            raise KeyError(f"uncommitted artifact producer set: {digest}") from exc
        try:
            data = json.loads(encoded)
            if not isinstance(data, dict) or set(data) != {
                "schema", "effect_id", "coordinate", "descriptor_digest", "artifacts"
            }:
                raise ValueError
            if data["schema"] != "lockstep.artifact-producer-set-commit/v1":
                raise ValueError
            effect_id = _text(data["effect_id"], "producer effect_id")
            coordinate = _coordinate_from_data(data["coordinate"])
            descriptor_digest = _digest(
                data["descriptor_digest"], "descriptor digest"
            )
            if self._set_digest(effect_id, coordinate, descriptor_digest) != digest:
                raise ValueError
            artifacts = data["artifacts"]
            if not isinstance(artifacts, list) or len(artifacts) > self._limits.max_artifacts_per_set:
                raise ValueError
            for item in artifacts:
                if not isinstance(item, dict) or set(item) != {"name", "artifact_ref"}:
                    raise ValueError
                _text(item["name"], "declared artifact name")
                ArtifactRef.parse(item["artifact_ref"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactError("invalid artifact producer set") from exc
        return data

    def list_for_producer(
        self,
        effect_id: str,
        coordinate: NativeCoordinate,
        descriptor_digest: str,
        declared_names: Iterable[str],
    ) -> tuple[ArtifactRef, ...]:
        values: list[ArtifactRef] = []
        names = take_bounded(
            declared_names,
            self._limits.max_artifacts_per_set,
            "artifact lookup names",
        )
        set_digest = self._set_digest(effect_id, coordinate, descriptor_digest)
        try:
            committed = self._read_committed_set(set_digest)
        except KeyError:
            return ()
        committed_names = tuple(item["name"] for item in committed["artifacts"])
        if tuple(names) != committed_names:
            # Exact bounded lookup may request a declared subset, but never
            # interpret a partial on-disk set as committed.
            if any(name not in committed_names for name in names):
                return ()
        for name in names:
            path = self._key_path(effect_id, coordinate, descriptor_digest, name)
            if not path.exists() and not path.is_symlink():
                continue
            encoded = _read_regular(path, max_bytes=self._limits.max_manifest_bytes)
            try:
                data = json.loads(encoded)
                ref = ArtifactRef.parse(data["artifact_ref"])
                record = self.read(ref)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ArtifactError("invalid artifact producer key") from exc
            if (
                record.producer_effect_id != effect_id
                or record.producer_coordinate != coordinate
                or record.descriptor_digest != descriptor_digest
                or record.declared_name != name
            ):
                raise ArtifactProvenanceError(
                    "artifact producer key does not match its exact provenance"
                )
            values.append(ref)
        return tuple(values)
