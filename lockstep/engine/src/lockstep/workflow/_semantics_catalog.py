"""Immutable catalog and compiled-bundle values for workflow semantics."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Mapping, Protocol, TypeAlias  # noqa: UP035

from .ir import FragmentIR, freeze

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
YamlgraphStateType: TypeAlias = Literal[
    "str", "string", "int", "integer", "float", "bool", "boolean",
    "list", "dict", "any",
]
_YAMLGRAPH_STATE_TYPES = frozenset({
    "str", "string", "int", "integer", "float", "bool", "boolean",
    "list", "dict", "any",
})
_STATE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_STATE_NAMES = frozenset({
    "lockstep_outcome", "lockstep_continue", "current_step",
    "_loop_counts", "_loop_limit_reached",
})


def _canonical_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a canonical contained POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must be a canonical contained POSIX path")
    return value


def _exact_sha256(content: bytes, claimed: str) -> None:
    if not isinstance(content, bytes):
        raise TypeError("compiled file content must be bytes")
    if not _SHA256.fullmatch(claimed) or hashlib.sha256(content).hexdigest() != claimed:
        raise ValueError("compiled file sha256 does not match its exact content")


def _manifest_bundle_sha256(
    root_relative_path: str, files: tuple[tuple[str, str], ...]
) -> str:
    digest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
    digest.update(root_relative_path.encode("utf-8"))
    digest.update(b"\0")
    for relative_path, sha256 in sorted(files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class ChildArtifactContract:
    handle: str
    fixed_source: str
    declared_name: str
    media_type: str
    producer_logical_id: str
    producer_result_state_key: str

    def __post_init__(self) -> None:
        for label, value in (
            ("artifact handle", self.handle),
            ("artifact declaration", self.declared_name),
            ("producer logical id", self.producer_logical_id),
            ("producer result state key", self.producer_result_state_key),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"child {label} must be non-empty text")
        if not isinstance(self.fixed_source, str) or not self.fixed_source:
            raise ValueError("child artifact source must be non-empty text")
        if not isinstance(self.media_type, str) or "/" not in self.media_type:
            raise ValueError("child artifact media type is invalid")


@dataclass(frozen=True)
class ChildWorkflowContract:
    """Closed child surface supplied by a caller-owned catalog."""

    outcomes: tuple[str, ...]
    exports: Mapping[str, ChildArtifactContract] = field(default_factory=dict)
    non_artifact_writes: tuple[str, ...] = ()
    state_inputs: Mapping[str, YamlgraphStateType] = field(default_factory=dict)
    state_exports: Mapping[str, YamlgraphStateType] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "exports", freeze(self.exports))
        object.__setattr__(self, "non_artifact_writes", tuple(self.non_artifact_writes))
        inputs = dict(self.state_inputs)
        exports = dict(self.state_exports)
        for name, state_type in (*inputs.items(), *exports.items()):
            if not isinstance(name, str) or not _STATE_NAME.fullmatch(name):
                raise ValueError("child state names must be logical state identifiers")
            if name in _RESERVED_STATE_NAMES or name.startswith("lockstep_"):
                raise ValueError(f"child state name {name!r} is reserved")
            if state_type not in _YAMLGRAPH_STATE_TYPES:
                raise ValueError(f"unsupported yamlgraph state type: {state_type!r}")
        for name in inputs.keys() & exports.keys():
            if inputs[name] != exports[name]:
                raise ValueError(
                    f"child state {name!r} has different types at input and export"
                )
        object.__setattr__(self, "state_inputs", freeze(inputs))
        object.__setattr__(self, "state_exports", freeze(exports))


@dataclass(frozen=True, slots=True)
class CatalogFile:
    relative_path: str
    content: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        _canonical_relative_path(self.relative_path)
        _exact_sha256(self.content, self.sha256)

    @classmethod
    def build(cls, relative_path: str, content: bytes) -> CatalogFile:
        if not isinstance(content, bytes):
            raise TypeError("compiled file content must be bytes")
        return cls(relative_path, content, hashlib.sha256(content).hexdigest())


@dataclass(frozen=True, slots=True)
class BundleDependency:
    kind: Literal["workflow", "fragment"]
    logical_name: str
    use_pointer: str
    definition_sha256: str
    compiled_sha256: str
    generated_root: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"workflow", "fragment"}:
            raise ValueError("unsupported compiled bundle dependency kind")
        if not isinstance(self.logical_name, str) or not self.logical_name:
            raise ValueError("bundle dependency logical_name must be non-empty")
        if not isinstance(self.use_pointer, str) or not self.use_pointer.startswith("/"):
            raise ValueError("bundle dependency use_pointer must be an absolute pointer")
        for digest in (self.definition_sha256, self.compiled_sha256):
            if not _SHA256.fullmatch(digest):
                raise ValueError("bundle dependency digest must be lowercase SHA-256")
        if self.kind == "workflow" and self.generated_root is None:
            raise ValueError("workflow dependency requires a generated root")
        if self.kind == "fragment" and self.generated_root is not None:
            raise ValueError("fragment dependency may not carry a generated root")
        if self.generated_root is not None:
            _canonical_relative_path(self.generated_root)


@dataclass(frozen=True, slots=True)
class CanonicalCompiledBundle:
    root_relative_path: str
    files: tuple[CatalogFile, ...]
    bundle_sha256: str
    compiler_version: str
    dependencies: tuple[BundleDependency, ...] = ()

    def __post_init__(self) -> None:
        _canonical_relative_path(self.root_relative_path)
        files = tuple(sorted(self.files, key=lambda item: item.relative_path))
        paths = tuple(item.relative_path for item in files)
        if len(paths) != len(set(paths)):
            raise ValueError("compiled bundle contains a duplicate file path")
        if self.root_relative_path not in paths:
            raise ValueError("compiled bundle root is missing from files")
        expected = _manifest_bundle_sha256(
            self.root_relative_path,
            tuple((item.relative_path, item.sha256) for item in files),
        )
        if self.bundle_sha256 != expected:
            raise ValueError("compiled bundle sha256 does not match its manifest")
        if self.compiler_version != "1":
            raise ValueError("compiled bundle compiler_version must be exactly '1'")
        dependencies = tuple(
            sorted(
                self.dependencies,
                key=lambda item: (item.use_pointer, item.kind, item.logical_name),
            )
        )
        if len(dependencies) != len(
            {(item.use_pointer, item.kind, item.logical_name) for item in dependencies}
        ):
            raise ValueError("compiled bundle contains duplicate dependency uses")
        file_paths = set(paths)
        for item in dependencies:
            if item.generated_root is not None and item.generated_root not in file_paths:
                raise ValueError("bundle dependency generated root is missing")
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "dependencies", dependencies)

    @classmethod
    def build(
        cls,
        *,
        root_relative_path: str,
        files: tuple[CatalogFile, ...],
        compiler_version: str,
        dependencies: tuple[BundleDependency, ...] = (),
    ) -> CanonicalCompiledBundle:
        frozen_files = tuple(files)
        return cls(
            root_relative_path,
            frozen_files,
            _manifest_bundle_sha256(
                root_relative_path,
                tuple((item.relative_path, item.sha256) for item in frozen_files),
            ),
            compiler_version,
            tuple(dependencies),
        )


@dataclass(frozen=True, slots=True)
class ResolvedChild:
    logical_name: str
    contract: ChildWorkflowContract
    source_definition_sha256: str
    standalone: CanonicalCompiledBundle

    def __post_init__(self) -> None:
        if not isinstance(self.logical_name, str) or not self.logical_name:
            raise ValueError("resolved child logical_name must be non-empty")
        if not _SHA256.fullmatch(self.source_definition_sha256):
            raise ValueError("resolved child source_definition_sha256 is invalid")


@dataclass(frozen=True, slots=True)
class ResolvedFragment:
    logical_path: str
    source_definition_sha256: str
    fragment: FragmentIR

    def __post_init__(self) -> None:
        _canonical_relative_path(self.logical_path)
        if not _SHA256.fullmatch(self.source_definition_sha256):
            raise ValueError("resolved fragment source_definition_sha256 is invalid")
        if not isinstance(self.fragment, FragmentIR):
            raise TypeError("resolved fragment requires closed FragmentIR")


@dataclass(frozen=True, slots=True)
class ResolvedCatalog:
    children: Mapping[str, ResolvedChild] = field(default_factory=dict)
    fragments: Mapping[str, ResolvedFragment] = field(default_factory=dict)

    def __post_init__(self) -> None:
        children = dict(self.children)
        fragments = dict(self.fragments)
        if any(key != child.logical_name for key, child in children.items()):
            raise ValueError("resolved child catalog key must equal logical_name")
        if any(key != fragment.logical_path for key, fragment in fragments.items()):
            raise ValueError("resolved fragment catalog key must equal logical_path")
        object.__setattr__(self, "children", freeze(children))
        object.__setattr__(self, "fragments", freeze(fragments))

    def contract_for(self, name: str) -> ChildWorkflowContract | None:
        child = self.children.get(name)
        return child.contract if child is not None else None

    def child_for(self, name: str) -> ResolvedChild | None:
        return self.children.get(name)

    def fragment_for(self, logical_path: str) -> ResolvedFragment | None:
        return self.fragments.get(logical_path)


class WorkflowCatalog(Protocol):
    def contract_for(self, name: str) -> ChildWorkflowContract | None: ...


@dataclass(frozen=True)
class InMemoryWorkflowCatalog:
    """Small immutable catalog useful to compilers and tests."""

    contracts: Mapping[str, ChildWorkflowContract]

    def __post_init__(self) -> None:
        object.__setattr__(self, "contracts", freeze(self.contracts))

    def contract_for(self, name: str) -> ChildWorkflowContract | None:
        return self.contracts.get(name)
