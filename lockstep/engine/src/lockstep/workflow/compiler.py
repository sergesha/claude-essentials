"""Public pure compiler for validated Workflow DSL input."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Literal, Mapping

from lockstep.recipe.profile import (
    CompilerProvenance,
    _create_compiler_provenance,
    check_recipe_bytes,
)
from lockstep.recipe.authority import canonical_execution_bytes
from lockstep.recipe.yamlgraph_adapter import validate_compiler_bundle

from .canonical import canonical_json, canonical_yaml
from .lowering import lower_workflow
from .schema import MarkedDocument, parse_workflow
from .semantics import (
    BundleDependency,
    CanonicalCompiledBundle,
    CatalogFile,
    ValidatedWorkflow,
    WorkflowCatalog,
    _canonical_relative_path,
    _exact_sha256,
    _manifest_bundle_sha256,
    validate_semantics,
)


@dataclass(frozen=True)
class DependencyEntry:
    kind: Literal["workflow", "fragment"]
    logical_name: str
    use_pointer: str
    definition_sha256: str
    compiled_sha256: str
    generated_root: str | None = None

    def __post_init__(self) -> None:
        # Reuse the resolved-catalog invariant so a manifest cannot describe a
        # different dependency kind/root relation than downstream composition.
        BundleDependency(
            self.kind,
            self.logical_name,
            self.use_pointer,
            self.definition_sha256,
            self.compiled_sha256,
            self.generated_root,
        )


@dataclass(frozen=True)
class DependencyManifest:
    schema: Literal["lockstep.workflow-dependencies/v1"]
    compiler_version: Literal["1"]
    root_name: str
    root_source_sha256: str
    entries: tuple[DependencyEntry, ...]

    def __post_init__(self) -> None:
        if self.schema != "lockstep.workflow-dependencies/v1":
            raise ValueError("unsupported dependency manifest schema")
        if self.compiler_version != "1":
            raise ValueError("dependency manifest compiler_version must be exactly '1'")
        if not isinstance(self.root_name, str) or not self.root_name:
            raise ValueError("dependency manifest root name must be non-empty")
        if (
            len(self.root_source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.root_source_sha256)
        ):
            raise ValueError("dependency manifest root digest must be lowercase SHA-256")
        entries = tuple(self.entries)
        canonical = tuple(
            sorted(entries, key=lambda item: (item.use_pointer, item.kind, item.logical_name))
        )
        if entries != canonical:
            raise ValueError("dependency manifest entries must be canonically sorted")
        identities = tuple(
            (item.use_pointer, item.kind, item.logical_name) for item in entries
        )
        if len(identities) != len(set(identities)):
            raise ValueError("dependency manifest contains duplicate dependency uses")
        object.__setattr__(self, "entries", entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "compiler_version": self.compiler_version,
            "root": {
                "name": self.root_name,
                "source_sha256": self.root_source_sha256,
            },
            "dependencies": [
                {
                    "kind": item.kind,
                    "logical_name": item.logical_name,
                    "use_pointer": item.use_pointer,
                    "definition_sha256": item.definition_sha256,
                    "compiled_sha256": item.compiled_sha256,
                    "generated_root": item.generated_root,
                }
                for item in self.entries
            ],
        }


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    relative_path: str
    content: bytes = field(repr=False)
    sha256: str
    role: Literal["specialized-child"] = "specialized-child"

    def __post_init__(self) -> None:
        _canonical_relative_path(self.relative_path)
        _exact_sha256(self.content, self.sha256)
        if self.role != "specialized-child":
            raise ValueError("unsupported generated file role")

    @classmethod
    def build(
        cls,
        relative_path: str,
        content: bytes,
        *,
        role: Literal["specialized-child"] = "specialized-child",
    ) -> "GeneratedFile":
        if not isinstance(content, bytes):
            raise TypeError("generated file content must be bytes")
        return cls(relative_path, content, hashlib.sha256(content).hexdigest(), role)


def generated_bundle_sha256(
    root_relative_path: str,
    recipe_bytes: bytes,
    generated_files: tuple[GeneratedFile, ...],
) -> str:
    """Bind the complete executable file set through a canonical manifest."""
    _canonical_relative_path(root_relative_path)
    if not isinstance(recipe_bytes, bytes):
        raise TypeError("root recipe content must be bytes")
    generated = tuple(generated_files)
    paths = (root_relative_path, *(item.relative_path for item in generated))
    if len(paths) != len(set(paths)):
        raise ValueError("compiled result contains a duplicate generated path")
    return _manifest_bundle_sha256(
        root_relative_path,
        (
            (root_relative_path, hashlib.sha256(recipe_bytes).hexdigest()),
            *((item.relative_path, item.sha256) for item in generated),
        ),
    )


@dataclass(frozen=True, slots=True)
class CompilationResult:
    root_relative_path: str
    recipe_bytes: bytes
    generated_files: tuple[GeneratedFile, ...]
    source_map_bytes: bytes
    dependency_manifest_bytes: bytes
    dependency_manifest: DependencyManifest
    digest: str
    bundle_sha256: str
    compiler_provenance: CompilerProvenance

    def __post_init__(self) -> None:
        generated = tuple(sorted(self.generated_files, key=lambda item: item.relative_path))
        expected_root = hashlib.sha256(self.recipe_bytes).hexdigest()
        if self.digest != expected_root:
            raise ValueError("compilation digest does not match root recipe bytes")
        expected_bundle = generated_bundle_sha256(
            self.root_relative_path, self.recipe_bytes, generated
        )
        if self.bundle_sha256 != expected_bundle:
            raise ValueError("compilation bundle_sha256 does not match executable files")
        proof_files = {
            item.relative_path: item.canonical_execution_bytes
            for item in self.compiler_provenance.files
        }
        expected_files = {
            self.root_relative_path: canonical_execution_bytes(
                self.recipe_bytes, logical_path=self.root_relative_path
            ),
            **{
                item.relative_path: canonical_execution_bytes(
                    item.content, logical_path=item.relative_path
                )
                for item in generated
            },
        }
        if proof_files != expected_files:
            raise ValueError("compiler provenance does not match executable file set")
        if self.compiler_provenance.source_bundle_sha256 != self.bundle_sha256:
            raise ValueError("compiler provenance does not bind source bundle digest")
        expected_manifest_bytes = canonical_json(self.dependency_manifest.to_dict())
        if self.dependency_manifest_bytes != expected_manifest_bytes:
            raise ValueError(
                "dependency manifest bytes do not match the immutable manifest"
            )
        object.__setattr__(self, "generated_files", generated)

    @property
    def executable_files(self) -> Mapping[str, bytes]:
        return {
            self.root_relative_path: self.recipe_bytes,
            **{item.relative_path: item.content for item in self.generated_files},
        }

    def as_catalog_bundle(self) -> CanonicalCompiledBundle:
        """Freeze this exact result for deterministic downstream composition."""

        return CanonicalCompiledBundle.build(
            root_relative_path=self.root_relative_path,
            files=tuple(
                CatalogFile.build(path, content)
                for path, content in self.executable_files.items()
            ),
            compiler_version=self.dependency_manifest.compiler_version,
            dependencies=tuple(
                BundleDependency(
                    item.kind,
                    item.logical_name,
                    item.use_pointer,
                    item.definition_sha256,
                    item.compiled_sha256,
                    item.generated_root,
                )
                for item in self.dependency_manifest.entries
            ),
        )


def compile_workflow(
    workflow: ValidatedWorkflow, catalog: WorkflowCatalog
) -> CompilationResult:
    if not isinstance(workflow, ValidatedWorkflow):
        raise TypeError("compile_workflow requires ValidatedWorkflow")
    document, source_map, lowered_files, lowered_dependencies = lower_workflow(
        workflow, catalog
    )
    recipe_bytes = canonical_yaml(document)
    generated_files = tuple(
        GeneratedFile(item.relative_path, item.content, item.sha256)
        for item in lowered_files
    )
    proof = _create_compiler_provenance(recipe_bytes, context="compiler-output")
    profile_errors, _warnings = check_recipe_bytes(recipe_bytes, proof)
    if profile_errors:
        raise ValueError(
            "compiler produced an invalid Lockstep recipe: "
            + "; ".join(profile_errors)
        )
    for item in generated_files:
        generated_proof = _create_compiler_provenance(
            item.content, context="compiler-output"
        )
        generated_errors, _generated_warnings = check_recipe_bytes(
            item.content, generated_proof
        )
        if generated_errors:
            raise ValueError(
                f"compiler produced an invalid generated recipe {item.relative_path!r}: "
                + "; ".join(generated_errors)
            )
    source_map_bytes = canonical_json(source_map)
    manifest = DependencyManifest(
        "lockstep.workflow-dependencies/v1",
        "1",
        workflow.workflow.name,
        workflow.workflow.source_sha256,
        tuple(
            DependencyEntry(
                kind=item.kind,
                logical_name=item.logical_name,
                use_pointer=item.use_pointer,
                definition_sha256=item.definition_sha256,
                compiled_sha256=item.compiled_sha256,
                generated_root=item.generated_root,
            )
            for item in sorted(
                lowered_dependencies,
                key=lambda entry: (entry.use_pointer, entry.kind, entry.logical_name),
            )
        ),
    )
    dependency_bytes = canonical_json(manifest.to_dict())
    root_relative_path = f"{workflow.workflow.name}.recipe.yaml"
    digest = hashlib.sha256(recipe_bytes).hexdigest()
    bundle_sha256 = generated_bundle_sha256(
        root_relative_path, recipe_bytes, generated_files
    )
    bundle_proof = _create_compiler_provenance(
        recipe_bytes,
        context="compiler-output",
        root_relative_path=root_relative_path,
        generated_files={item.relative_path: item.content for item in generated_files},
        execution_recipe_bytes=canonical_execution_bytes(
            recipe_bytes, logical_path=root_relative_path
        ),
        execution_generated_files={
            item.relative_path: canonical_execution_bytes(
                item.content, logical_path=item.relative_path
            )
            for item in generated_files
        },
        source_bundle_sha256=bundle_sha256,
    )
    execution_files = {
        root_relative_path: canonical_execution_bytes(
            recipe_bytes, logical_path=root_relative_path
        ),
        **{
            item.relative_path: canonical_execution_bytes(
                item.content, logical_path=item.relative_path
            )
            for item in generated_files
        },
    }
    native_ok, native_detail = validate_compiler_bundle(
        root_relative_path=root_relative_path,
        execution_files=execution_files,
        provenance=bundle_proof,
    )
    if not native_ok:
        raise ValueError(
            "compiler produced a bundle rejected by the final native gate: "
            + native_detail
        )
    return CompilationResult(
        root_relative_path=root_relative_path,
        recipe_bytes=recipe_bytes,
        generated_files=generated_files,
        source_map_bytes=source_map_bytes,
        dependency_manifest_bytes=dependency_bytes,
        dependency_manifest=manifest,
        digest=digest,
        bundle_sha256=bundle_sha256,
        compiler_provenance=bundle_proof,
    )


def compile_workflow_document(
    document: MarkedDocument, catalog: WorkflowCatalog
) -> tuple[ValidatedWorkflow, CompilationResult]:
    """Validate and compile one already-loaded workflow document."""

    workflow = parse_workflow(document)
    validated = validate_semantics(workflow, catalog)
    return validated, compile_workflow(validated, catalog)
