"""Authority-domain values and owner-reviewed executable policy."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from lockstep.runtime.recipe_bundles import (
    MaterializedRecipe,
    RecipeBundleRef,
    ValidatedDependencyDAG,
)

if TYPE_CHECKING:
    from lockstep.recipe.profile import CompilerProvenance
    from lockstep.runtime.recipe_bundles import RecipeBundleStore


class RecipeAuthorityError(ValueError):
    """Recipe bytes cannot enter the executable workflow boundary."""


class AuthorityDenied(RecipeAuthorityError):
    """A recipe requests executable authority without an exact owner grant."""


@dataclass(frozen=True)
class RecipeLimits:
    max_source_bytes: int = 4 * 1024 * 1024
    max_file_bytes: int = 1024 * 1024
    max_files: int = 256
    max_depth: int = 64
    max_nodes: int = 50_000
    max_container_items: int = 10_000
    max_scalar_bytes: int = 2 * 1024 * 1024
    max_integer_abs: int = 2**63 - 1

    def __post_init__(self) -> None:
        if (
            min(
                self.max_source_bytes,
                self.max_file_bytes,
                self.max_files,
                self.max_depth,
                self.max_nodes,
                self.max_container_items,
                self.max_scalar_bytes,
                self.max_integer_abs,
            )
            <= 0
        ):
            raise ValueError("recipe limits must be positive")


@dataclass(frozen=True, order=True)
class CanonicalRecipeFile:
    path: str
    bytes: bytes
    sha256: str


@dataclass(frozen=True, order=True)
class AuthorityRequirement:
    """One exact compile/runtime executable surface found in canonical YAML."""

    sha256: str
    kind: Literal["python", "shell"]
    tool_name: str
    descriptor: tuple[tuple[str, object], ...]
    uses: tuple[str, ...]


@dataclass(frozen=True, order=True)
class OwnerReviewedGrant:
    """TCB configuration, never a value accepted from recipe YAML."""

    recipe_sha256: str
    requirement_sha256: str
    authority: Literal["os_user_execution"]

    def __post_init__(self) -> None:
        for value in (self.recipe_sha256, self.requirement_sha256):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(
                    "owner-reviewed grants require lowercase SHA-256 digests"
                )
        if self.authority != "os_user_execution":
            raise ValueError("local executable grants must name full os_user_execution")


@dataclass(frozen=True, order=True)
class OwnerReviewedPythonTarget:
    """Exact installed Lockstep callable admitted by owner configuration."""

    module: str
    function: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"lockstep(?:\.[A-Za-z_]\w*)+", self.module):
            raise ValueError(
                "reviewed Python targets must name an exact installed Lockstep module"
            )
        if not re.fullmatch(r"[A-Za-z_]\w*", self.function):
            raise ValueError("reviewed Python targets require an exact function name")


@dataclass(frozen=True)
class RecipeAuthorityPolicy:
    grants: tuple[OwnerReviewedGrant, ...] = ()
    python_targets: tuple[OwnerReviewedPythonTarget, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.grants, tuple) or not isinstance(
            self.python_targets, tuple
        ):
            raise TypeError("recipe authority policy entries must be tuples")

    def permits(self, recipe_sha256: str, requirement: AuthorityRequirement) -> bool:
        digest_granted = any(
            grant.recipe_sha256 == recipe_sha256
            and grant.requirement_sha256 == requirement.sha256
            and grant.authority == "os_user_execution"
            for grant in self.grants
        )
        if not digest_granted:
            return False
        if requirement.kind != "python":
            return True
        descriptor = dict(requirement.descriptor)
        try:
            target = OwnerReviewedPythonTarget(
                module=str(descriptor.get("module", "")),
                function=str(descriptor.get("function", "")),
            )
        except ValueError:
            return False
        return target in self.python_targets


@dataclass(frozen=True)
class AuthorizedRecipe:
    root: str
    files: tuple[CanonicalRecipeFile, ...]
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    authority_requirements: tuple[AuthorityRequirement, ...]
    source_bundle_sha256: str
    canonical_match_proof: CompilerProvenance | None = None

    def capture(self, store: RecipeBundleStore) -> AdmittedRecipe:
        """Publish these exact canonical bytes through the DAG-only store seam."""
        with tempfile.TemporaryDirectory(prefix="lockstep-canonical-recipe-") as raw:
            staging = Path(raw)
            for item in self.files:
                destination = staging / item.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.bytes)
            bundle = store.capture(staging, self.dependency_dag)
        return AdmittedRecipe(
            bundle=bundle,
            root=self.root,
            files=self.files,
            definition_sha256=self.definition_sha256,
            dependency_dag=self.dependency_dag,
            authority_requirements=self.authority_requirements,
        )


@dataclass(frozen=True)
class AuthorizedMaterialization:
    bundle: RecipeBundleRef
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    source_path: Path
    directory: Path


@dataclass(frozen=True)
class AdmittedRecipe:
    """Durable identity for one authorized canonical recipe bundle."""

    bundle: RecipeBundleRef
    root: str
    files: tuple[CanonicalRecipeFile, ...]
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    authority_requirements: tuple[AuthorityRequirement, ...]

    def materialize(self, store: RecipeBundleStore) -> AuthorizedMaterialization:
        materialized: MaterializedRecipe = store.materialize_for_compile(self.bundle)
        manifest = store.read_manifest(self.bundle)
        expected = tuple(
            (item.path, item.sha256, len(item.bytes)) for item in self.files
        )
        observed = tuple((item.path, item.sha256, item.size) for item in manifest.files)
        if manifest.root != self.root or observed != expected:
            raise RecipeAuthorityError(
                "admitted recipe bundle no longer matches its canonical definition"
            )
        return AuthorizedMaterialization(
            bundle=self.bundle,
            definition_sha256=self.definition_sha256,
            dependency_dag=self.dependency_dag,
            source_path=materialized.source_path,
            directory=materialized.directory,
        )


@dataclass(frozen=True)
class RecipeCandidate:
    """Canonical, closed content with no executable authority yet."""

    root: str
    files: tuple[CanonicalRecipeFile, ...]
    definition_sha256: str
    dependency_dag: ValidatedDependencyDAG
    authority_requirements: tuple[AuthorityRequirement, ...]
    source_bundle_sha256: str

    def authorize(self, policy: RecipeAuthorityPolicy) -> AuthorizedRecipe:
        if not isinstance(policy, RecipeAuthorityPolicy):
            raise TypeError("recipe authorization requires a RecipeAuthorityPolicy")
        denied = tuple(
            requirement
            for requirement in self.authority_requirements
            if not policy.permits(self.definition_sha256, requirement)
        )
        if denied:
            labels = ", ".join(
                f"{item.kind} tool {item.tool_name!r} ({item.sha256})"
                for item in denied
            )
            raise AuthorityDenied(
                "recipe executable authority denied: "
                f"{labels}; an exact owner-reviewed os_user_execution grant "
                "bound to this definition digest is required"
            )
        return AuthorizedRecipe(
            root=self.root,
            files=self.files,
            definition_sha256=self.definition_sha256,
            dependency_dag=self.dependency_dag,
            authority_requirements=self.authority_requirements,
            source_bundle_sha256=self.source_bundle_sha256,
        )
