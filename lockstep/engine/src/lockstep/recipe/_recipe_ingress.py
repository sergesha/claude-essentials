"""Filesystem ingress and recursive recipe dependency-DAG traversal."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from lockstep.runtime.owner_state import StorageLimitExceeded
from lockstep.runtime.recipe_bundles import (
    ValidatedDependencyDAG,
    open_recipe_source_root,
    read_recipe_source_file,
    safe_recipe_relative_path,
)

from ._authority_models import (
    AuthorityRequirement,
    CanonicalRecipeFile,
    RecipeAuthorityError,
    RecipeCandidate,
    RecipeLimits,
)
from ._recipe_document import (
    _canonical_bytes,
    _profile_document,
    recipe_definition_sha256,
)
from ._strict_yaml import _decode_document

_RecipeReader = Callable[[str, int], bytes]


def decode_recipe_document(
    data: bytes,
    *,
    logical: str,
    limits: RecipeLimits | None = None,
) -> dict[str, object]:
    """Decode already captured recipe bytes through the strict YAML boundary."""

    return _decode_document(data, limits or RecipeLimits(), logical)


def _candidate_from_documents(
    root: str,
    documents: Mapping[str, bytes],
    source_hashes: Mapping[str, str],
    requirements: list[AuthorityRequirement],
    limits: RecipeLimits,
) -> RecipeCandidate:
    files = tuple(
        CanonicalRecipeFile(
            path=path,
            bytes=data,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        for path, data in sorted(documents.items())
    )
    definition_sha256 = recipe_definition_sha256(
        root,
        ((item.path, item.sha256, len(item.bytes)) for item in files),
    )
    dag = ValidatedDependencyDAG.from_validated(
        root,
        (item.path for item in files),
        max_files=limits.max_files,
        max_dependencies=limits.max_files - 1,
    )
    return RecipeCandidate(
        root=root,
        files=files,
        definition_sha256=definition_sha256,
        dependency_dag=dag,
        authority_requirements=tuple(sorted(requirements)),
        source_bundle_sha256=_source_bundle_sha256(root, source_hashes),
    )


def _source_bundle_sha256(root: str, source_hashes: Mapping[str, str]) -> str:
    manifest = hashlib.sha256(b"lockstep.compiled-bundle/v1\0")
    manifest.update(root.encode("utf-8"))
    manifest.update(b"\0")
    for path, sha256 in sorted(source_hashes.items()):
        manifest.update(path.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(sha256.encode("ascii"))
        manifest.update(b"\0")
    return manifest.hexdigest()


def _inspect_recipe(
    root: str, read: _RecipeReader, limits: RecipeLimits
) -> RecipeCandidate:
    root = safe_recipe_relative_path(root)
    active: set[str] = set()
    documents: dict[str, bytes] = {}
    source_hashes: dict[str, str] = {}
    requirements: list[AuthorityRequirement] = []
    total_bytes = 0

    def visit(logical: str) -> None:
        nonlocal total_bytes
        if logical in documents:
            return
        if logical in active:
            raise RecipeAuthorityError(
                f"recursive subgraph dependency is not a DAG: {logical}"
            )
        if len(documents) + len(active) >= limits.max_files:
            raise RecipeAuthorityError(f"recipe files exceed {limits.max_files}")
        remaining = limits.max_source_bytes - total_bytes
        if remaining <= 0:
            raise RecipeAuthorityError(
                f"recipe source bytes exceed {limits.max_source_bytes}"
            )
        try:
            data = read(logical, min(remaining, limits.max_file_bytes))
        except StorageLimitExceeded as exc:
            raise RecipeAuthorityError(
                "recipe source bytes exceed configured admission limit"
            ) from exc
        total_bytes += len(data)
        source_hashes[logical] = hashlib.sha256(data).hexdigest()
        document = _decode_document(data, limits, logical)
        profile = _profile_document(document, logical)
        active.add(logical)
        try:
            for dependency in profile.dependencies:
                visit(dependency)
        finally:
            active.remove(logical)
        documents[logical] = _canonical_bytes(document)
        requirements.extend(profile.requirements)

    visit(root)
    return _candidate_from_documents(
        root, documents, source_hashes, requirements, limits
    )


def inspect_recipe_bytes(
    root: str,
    sources: Mapping[str, bytes],
    *,
    limits: RecipeLimits | None = None,
) -> RecipeCandidate:
    """Inspect one closed recipe DAG from already captured exact bytes."""

    captured = dict(sources)

    def read(logical: str, max_bytes: int) -> bytes:
        try:
            data = captured[logical]
        except KeyError as exc:
            raise FileNotFoundError(logical) from exc
        if not isinstance(data, bytes):
            raise TypeError("captured recipe source must be bytes")
        if len(data) > max_bytes:
            raise RecipeAuthorityError(
                "recipe source bytes exceed configured admission limit"
            )
        return data

    try:
        return _inspect_recipe(root, read, limits or RecipeLimits())
    except FileNotFoundError as exc:
        raise RecipeAuthorityError(f"recipe dependency not found: {exc}") from exc


class StrictRecipeIngress:
    """Read and close one recipe definition without invoking yamlgraph."""

    def __init__(
        self,
        source_root: str | Path,
        *,
        limits: RecipeLimits | None = None,
    ) -> None:
        self._source_root = Path(source_root)
        self._limits = limits or RecipeLimits()

    def inspect(self, root: str) -> RecipeCandidate:
        root_fd = open_recipe_source_root(self._source_root)

        def read(logical: str, max_bytes: int) -> bytes:
            return read_recipe_source_file(
                root_fd, PurePosixPath(logical), max_bytes=max_bytes
            )

        try:
            return _inspect_recipe(root, read, self._limits)
        except FileNotFoundError as exc:
            raise RecipeAuthorityError(f"recipe dependency not found: {exc}") from exc
        finally:
            os.close(root_fd)
