from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
import yaml

from lockstep.workflow.compiler import (
    DependencyEntry,
    DependencyManifest,
    GeneratedFile,
    compile_workflow,
    generated_bundle_sha256,
)
from lockstep.workflow.canonical import canonical_yaml
from lockstep.workflow.freshness import FreshnessError, verify_canonical_match
from lockstep.workflow.ir import FragmentIR
from lockstep.workflow.schema import load_workflow, parse_workflow
from lockstep.workflow.semantics import (
    CanonicalCompiledBundle,
    BundleDependency,
    CatalogFile,
    ChildWorkflowContract,
    ResolvedCatalog,
    ResolvedChild,
    ResolvedFragment,
    validate_semantics,
)
from lockstep.recipe.profile import check_recipe_full
from lockstep.recipe.profile import _create_compiler_provenance
from lockstep.recipe.authority import StrictRecipeIngress, canonical_execution_bytes
from lockstep.recipe.yamlgraph_adapter import validate_compiler_bundle


_EMPTY_RECIPE = b"version: '1'\n"


def test_resolved_catalog_is_a_frozen_io_free_lookup() -> None:
    """Catches mutable catalog aliases or lowering-time loader authority."""
    root_file = CatalogFile.build("child.recipe.yaml", _EMPTY_RECIPE)
    bundle = CanonicalCompiledBundle.build(
        root_relative_path="child.recipe.yaml",
        files=(root_file,),
        compiler_version="1",
    )
    contract = ChildWorkflowContract(
        outcomes=("pass",),
        state_inputs={"brief": "dict"},
        state_exports={"verdict": "str"},
    )
    child = ResolvedChild(
        logical_name="review",
        contract=contract,
        source_definition_sha256="1" * 64,
        standalone=bundle,
    )
    fragment_source = {
        "fragment": {
            "entry": "done",
            "exits": {"pass": "done"},
            "effects": {"mode": "read-only", "writes": []},
        },
        "nodes": {"done": {"type": "passthrough"}},
        "edges": [],
    }
    fragment = ResolvedFragment(
        logical_path="fragments/review.yaml",
        source_definition_sha256="2" * 64,
        fragment=FragmentIR.parse(fragment_source),
    )
    catalog = ResolvedCatalog(
        children={"review": child}, fragments={"fragments/review.yaml": fragment}
    )

    assert catalog.contract_for("review") is contract
    assert catalog.child_for("review") is child
    assert catalog.fragment_for("fragments/review.yaml") is fragment
    with pytest.raises(TypeError):
        catalog.children["other"] = child  # type: ignore[index]
    with pytest.raises(TypeError):
        fragment.fragment["new"] = "mutable"  # type: ignore[index]


def test_catalog_files_reject_traversal_and_digest_substitution() -> None:
    """Catches a bundle member escaping its materialization root or lying about bytes."""
    with pytest.raises(ValueError, match="canonical contained POSIX"):
        CatalogFile.build("../outside.recipe.yaml", b"x")
    with pytest.raises(ValueError, match="sha256"):
        CatalogFile("child.recipe.yaml", b"x", "0" * 64)


def test_canonical_bundle_sorts_files_rejects_duplicates_and_binds_paths() -> None:
    """Catches ambiguous bundles and digests that ignore member identities."""
    root = CatalogFile.build("root.recipe.yaml", b"root")
    child = CatalogFile.build("nested/child.recipe.yaml", b"child")
    bundle = CanonicalCompiledBundle.build(
        root_relative_path="root.recipe.yaml",
        files=(child, root),
        compiler_version="1",
    )

    assert tuple(item.relative_path for item in bundle.files) == (
        "nested/child.recipe.yaml",
        "root.recipe.yaml",
    )
    assert bundle.bundle_sha256 != hashlib.sha256(b"rootchild").hexdigest()
    renamed_digest = generated_bundle_sha256(
        "renamed.recipe.yaml",
        b"root",
        (GeneratedFile.build("nested/child.recipe.yaml", b"child"),),
    )
    assert renamed_digest != bundle.bundle_sha256
    with pytest.raises(ValueError, match="duplicate"):
        CanonicalCompiledBundle.build(
            root_relative_path="root.recipe.yaml",
            files=(root, root),
            compiler_version="1",
        )
    dependency_fields = {
        "logical_name": "dependency",
        "use_pointer": "/flow/0",
        "definition_sha256": "a" * 64,
        "compiled_sha256": "b" * 64,
    }
    with pytest.raises(ValueError, match="workflow dependency requires"):
        BundleDependency(kind="workflow", generated_root=None, **dependency_fields)
    with pytest.raises(ValueError, match="fragment dependency may not"):
        BundleDependency(
            kind="fragment",
            generated_root="generated/child.recipe.yaml",
            **dependency_fields,
        )


def test_state_contract_rejects_unknown_types_and_incompatible_round_trip() -> None:
    """Catches a shared-state bridge whose child input/export types disagree."""
    with pytest.raises(ValueError, match="yamlgraph state type"):
        ChildWorkflowContract(("pass",), state_inputs={"brief": "json"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="different types"):
        ChildWorkflowContract(
            ("pass",),
            state_inputs={"value": "str"},
            state_exports={"value": "int"},
        )


def test_generated_file_hash_and_bundle_digest_are_exact_and_order_independent() -> None:
    """Catches unverified generated bytes and order-dependent manifest hashing."""
    first = GeneratedFile.build("generated/a.recipe.yaml", b"a")
    second = GeneratedFile.build("generated/b.recipe.yaml", b"b")
    forward = generated_bundle_sha256("root.recipe.yaml", b"root", (first, second))
    reverse = generated_bundle_sha256("root.recipe.yaml", b"root", (second, first))

    assert forward == reverse
    assert forward != generated_bundle_sha256(
        "root.recipe.yaml", b"root", (GeneratedFile.build(first.relative_path, b"A"), second)
    )
    with pytest.raises(ValueError, match="sha256"):
        GeneratedFile("generated/a.recipe.yaml", b"a", "0" * 64)


def test_dependency_manifest_freezes_entries_and_rejects_noncanonical_order() -> None:
    first = DependencyEntry(
        "fragment", "a", "/flow/0", "a" * 64, "b" * 64, None
    )
    second = DependencyEntry(
        "fragment", "b", "/flow/1", "c" * 64, "d" * 64, None
    )
    authored = [first, second]
    manifest = DependencyManifest(
        "lockstep.workflow-dependencies/v1", "1", "root", "e" * 64,
        authored,  # type: ignore[arg-type] - adversarial mutable caller
    )
    authored.clear()
    assert manifest.entries == (first, second)
    with pytest.raises(ValueError, match="canonically sorted"):
        DependencyManifest(
            "lockstep.workflow-dependencies/v1", "1", "root", "e" * 64,
            (second, first),
        )


def test_compiler_final_gate_enforces_strict_ingress_aggregate_file_limit() -> None:
    source_files: dict[str, bytes] = {}
    for index in range(257):
        path = f"member-{index}.recipe.yaml"
        if index < 256:
            document = {
                "version": "1.0",
                "name": f"member-{index}",
                "state": {},
                "nodes": {
                    "child": {
                        "type": "subgraph",
                        "graph": f"member-{index + 1}.recipe.yaml",
                        "mode": "direct",
                    }
                },
                "edges": [
                    {"from": "START", "to": "child"},
                    {"from": "child", "to": "END"},
                ],
            }
        else:
            document = {
                "version": "1.0",
                "name": f"member-{index}",
                "state": {},
                "nodes": {"done": {"type": "passthrough"}},
                "edges": [
                    {"from": "START", "to": "done"},
                    {"from": "done", "to": "END"},
                ],
            }
        source_files[path] = canonical_yaml(document)
    root_path = "member-0.recipe.yaml"
    execution_files = {
        path: canonical_execution_bytes(content, logical_path=path)
        for path, content in source_files.items()
    }
    provenance = _create_compiler_provenance(
        source_files[root_path],
        context="compiler-output",
        root_relative_path=root_path,
        generated_files={
            path: content for path, content in source_files.items()
            if path != root_path
        },
        execution_recipe_bytes=execution_files[root_path],
        execution_generated_files={
            path: content for path, content in execution_files.items()
            if path != root_path
        },
        source_bundle_sha256="f" * 64,
    )
    ok, message = validate_compiler_bundle(
        root_relative_path=root_path,
        execution_files=execution_files,
        provenance=provenance,
    )
    assert ok is False
    assert "exceed 256" in message


def test_freshness_rejects_an_extra_generated_file(tmp_path) -> None:
    """Catches freshness checks that compare only the generated root recipe."""
    source = tmp_path / "root.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: root\ndescription: root\n"
        "protect: ['**']\nflow: [{escalate: {}}]\n"
    )
    catalog = ResolvedCatalog()
    validated = validate_semantics(parse_workflow(load_workflow(source)), catalog)
    compiled = compile_workflow(validated, catalog)

    with pytest.raises(FreshnessError, match="generated file set"):
        verify_canonical_match(
            validated,
            catalog,
            compiled.recipe_bytes,
            candidate_generated_files={"generated/unexpected.recipe.yaml": b"x"},
            candidate_dependency_manifest_bytes=compiled.dependency_manifest_bytes,
        )


def test_direct_child_compilation_emits_a_manifest_bound_generated_bundle(tmp_path) -> None:
    """Catches a native child file omitted from result and dependency provenance."""
    child_source = tmp_path / "child.workflow.yaml"
    child_source.write_text(
        "workflow_version: '1'\nname: child\ndescription: child\n"
        "protect: ['**']\nflow: [{escalate: {}}]\n"
    )
    empty_catalog = ResolvedCatalog()
    child_ir = parse_workflow(load_workflow(child_source))
    child_result = compile_workflow(
        validate_semantics(child_ir, empty_catalog), empty_catalog
    )
    standalone = CanonicalCompiledBundle.build(
        root_relative_path=child_result.root_relative_path,
        files=(CatalogFile.build(child_result.root_relative_path, child_result.recipe_bytes),),
        compiler_version="1",
    )
    resolved_child = ResolvedChild(
        logical_name="child",
        contract=ChildWorkflowContract(("pass", "fail", "error")),
        source_definition_sha256=child_ir.source_sha256,
        standalone=standalone,
    )
    catalog = ResolvedCatalog(children={"child": resolved_child})
    parent_source = tmp_path / "parent.workflow.yaml"
    parent_source.write_text(
        "workflow_version: '1'\nname: parent\ndescription: parent\n"
        "protect: ['**']\nflow:\n"
        "  - call:\n      workflow: child\n      runner: codex\n"
    )
    parent_ir = parse_workflow(load_workflow(parent_source))

    result = compile_workflow(validate_semantics(parent_ir, catalog), catalog)

    assert len(result.generated_files) == 1
    generated = result.generated_files[0]
    assert result.executable_files[generated.relative_path] == generated.content
    assert result.bundle_sha256 == generated_bundle_sha256(
        result.root_relative_path, result.recipe_bytes, result.generated_files
    )
    assert len(result.dependency_manifest.entries) == 1
    entry = result.dependency_manifest.entries[0]
    assert entry.kind == "workflow"
    assert entry.logical_name == "child"
    assert entry.use_pointer == "/flow/0"
    assert entry.definition_sha256 == child_ir.source_sha256
    with pytest.raises(ValueError, match="manifest bytes"):
        replace(result, dependency_manifest_bytes=b"{}")
    assert entry.compiled_sha256 == generated_bundle_sha256(
        generated.relative_path, generated.content, ()
    )
    assert entry.generated_root == generated.relative_path


def test_compiler_rejects_a_child_dag_missing_a_native_subgraph_member(
    tmp_path,
) -> None:
    broken = (
        b"version: '1.0'\nname: broken\nnodes:\n"
        b"  nested: {type: subgraph, graph: missing.recipe.yaml, mode: direct}\n"
        b"edges: [{from: START, to: nested}, {from: nested, to: END}]\n"
    )
    catalog = ResolvedCatalog(children={
        "broken": ResolvedChild(
            "broken",
            ChildWorkflowContract(("pass", "fail", "error")),
            "9" * 64,
            CanonicalCompiledBundle.build(
                root_relative_path="broken.recipe.yaml",
                files=(CatalogFile.build("broken.recipe.yaml", broken),),
                compiler_version="1",
            ),
        )
    })
    source = tmp_path / "parent.workflow.yaml"
    source.write_text(
        "workflow_version: '1'\nname: parent\ndescription: parent\n"
        "protect: ['**']\nflow:\n"
        "  - call:\n      workflow: broken\n      runner: codex\n"
    )
    validated = validate_semantics(parse_workflow(load_workflow(source)), catalog)

    with pytest.raises(ValueError, match="unknown member|final native gate"):
        compile_workflow(validated, catalog)


def test_nested_children_emit_complete_transitive_manifest_and_exact_provenance(
    tmp_path,
) -> None:
    """Catches root-only manifests and provenance that omits a nested child."""

    def source(name: str, flow: str):
        path = tmp_path / f"{name}.workflow.yaml"
        path.write_text(
            "workflow_version: '1'\n"
            f"name: {name}\ndescription: {name}\nprotect: ['**']\nflow:\n{flow}"
        )
        return parse_workflow(load_workflow(path))

    empty = ResolvedCatalog()
    leaf_ir = source(
        "leaf", "  - step: work\n    task: Perform the work\n    exit: Work is complete\n"
    )
    leaf_result = compile_workflow(validate_semantics(leaf_ir, empty), empty)
    leaf = ResolvedChild(
        "leaf",
        ChildWorkflowContract(("pass", "fail", "error")),
        leaf_ir.source_sha256,
        leaf_result.as_catalog_bundle(),
    )
    child_catalog = ResolvedCatalog(children={"leaf": leaf})
    child_ir = source(
        "child",
        "  - call:\n      workflow: leaf\n      runner: reviewer\n",
    )
    child_result = compile_workflow(
        validate_semantics(child_ir, child_catalog), child_catalog
    )
    child = ResolvedChild(
        "child",
        ChildWorkflowContract(("pass", "fail", "error")),
        child_ir.source_sha256,
        child_result.as_catalog_bundle(),
    )
    parent_catalog = ResolvedCatalog(children={"child": child})
    parent_ir = source(
        "parent",
        "  - call:\n      workflow: child\n      runner: codex\n",
    )
    parent_result = compile_workflow(
        validate_semantics(parent_ir, parent_catalog), parent_catalog
    )

    assert [item.logical_name for item in parent_result.dependency_manifest.entries] == [
        "child",
        "leaf",
    ]
    assert [item.use_pointer for item in parent_result.dependency_manifest.entries] == [
        "/flow/0",
        "/flow/0/flow/0",
    ]
    generated_paths = {item.relative_path for item in parent_result.generated_files}
    assert all(
        item.generated_root in generated_paths
        for item in parent_result.dependency_manifest.entries
    )
    generated_documents = [
        yaml.safe_load(item.content) for item in parent_result.generated_files
    ]
    protected = [
        node["message"]["lockstep_effect"]
        for document in generated_documents
        for node in document["nodes"].values()
        if isinstance(node.get("message"), dict)
        and isinstance(node["message"].get("lockstep_effect"), dict)
    ]
    inner_scope = next(
        item for item in protected
        if item["kind"] == "scope" and item["runner_selector"] == "reviewer"
    )
    inner_work = next(
        item for item in protected
        if item["kind"] == "managed"
        and item["runner"]["selector"] == "reviewer"
    )
    assert len(inner_scope["ancestor_deadline_state_keys"]) == 1
    assert inner_work["scope_state_keys"] == [inner_scope["result_state_key"]]
    assert inner_scope["ancestor_deadline_state_keys"][0] not in inner_work[
        "scope_state_keys"
    ]

    emitted = tmp_path / "emitted"
    for relative_path, content in parent_result.executable_files.items():
        target = emitted / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    candidate = StrictRecipeIngress(emitted).inspect(parent_result.root_relative_path)
    canonical = tmp_path / "canonical"
    for item in candidate.files:
        target = canonical / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.bytes)
    errors, _warnings = check_recipe_full(
        canonical / candidate.root,
        provenance=parent_result.compiler_provenance,
    )
    assert errors == []

    nested = next(item for item in candidate.files if item.path != candidate.root)
    target = canonical / nested.path
    target.write_bytes(target.read_bytes() + b" ")
    errors, _warnings = check_recipe_full(
        canonical / candidate.root,
        provenance=parent_result.compiler_provenance,
    )
    assert any("provenance" in item for item in errors)


def test_parent_rebinds_transitive_fragment_digest_after_child_specialization(
    tmp_path,
) -> None:
    child_source = tmp_path / "child-fragment.workflow.yaml"
    child_source.write_text(
        "workflow_version: '1'\nname: child-fragment\ndescription: child fragment\n"
        "protect: ['**']\nflow:\n"
        "  - graph:\n"
        "      id: inspect\n"
        "      fragment:\n"
        "        entry: done\n"
        "        exits: {pass: done}\n"
        "        effects: {mode: read-only, writes: []}\n"
        "      state: {note: str}\n"
        "      nodes: {done: {type: passthrough, output: {note: checked}}}\n"
        "      edges: []\n"
    )
    empty = ResolvedCatalog()
    child_ir = parse_workflow(load_workflow(child_source))
    child_result = compile_workflow(validate_semantics(child_ir, empty), empty)
    child_fragment = next(
        item for item in child_result.dependency_manifest.entries
        if item.kind == "fragment"
    )
    catalog = ResolvedCatalog(children={
        "child-fragment": ResolvedChild(
            "child-fragment",
            ChildWorkflowContract(("pass", "fail", "error")),
            child_ir.source_sha256,
            child_result.as_catalog_bundle(),
        )
    })
    parent_source = tmp_path / "parent.workflow.yaml"
    parent_source.write_text(
        "workflow_version: '1'\nname: parent\ndescription: parent\n"
        "protect: ['**']\nflow:\n"
        "  - call:\n      workflow: child-fragment\n      runner: codex\n"
    )
    parent_ir = parse_workflow(load_workflow(parent_source))
    first = compile_workflow(validate_semantics(parent_ir, catalog), catalog)
    second = compile_workflow(validate_semantics(parent_ir, catalog), catalog)
    transitive = next(
        item for item in first.dependency_manifest.entries
        if item.kind == "fragment"
    )
    generated_root = next(
        item for item in first.generated_files
        if item.relative_path == next(
            dependency.generated_root
            for dependency in first.dependency_manifest.entries
            if dependency.kind == "workflow"
        )
    )
    specialized = yaml.safe_load(generated_root.content)
    fragment_nodes = {
        name for name in specialized["nodes"] if ".inspect." in name
    }
    fragment_state = {
        key
        for name in fragment_nodes
        for key in specialized["nodes"][name].get("output", {})
    }
    expected_projection = {
        "state": {
            key: specialized["state"][key]
            for key in specialized["state"]
            if key in fragment_state
        },
        "nodes": {
            key: specialized["nodes"][key]
            for key in specialized["nodes"]
            if key in fragment_nodes
        },
        "edges": [
            edge for edge in specialized["edges"]
            if edge.get("from") in fragment_nodes
            or edge.get("to") in fragment_nodes
        ],
    }
    independently_computed = hashlib.sha256(
        canonical_yaml(expected_projection)
    ).hexdigest()

    assert transitive.use_pointer == "/flow/0/flow/0"
    assert transitive.compiled_sha256 != child_fragment.compiled_sha256
    assert transitive.compiled_sha256 == independently_computed
    assert transitive.compiled_sha256 == next(
        item.compiled_sha256 for item in second.dependency_manifest.entries
        if item.kind == "fragment"
    )
