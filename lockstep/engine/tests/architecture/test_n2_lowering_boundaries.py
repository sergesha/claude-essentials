"""N2 ownership boundary for workflow lowering."""

from __future__ import annotations

import ast
import importlib
import inspect
import subprocess
import sys
import typing
from pathlib import Path

SOURCE = Path(__file__).parents[2] / "src" / "lockstep" / "workflow"

ROLE_METHODS = {
    "_lowering_core": {
        "outcome_target",
        "declare_generated_state",
        "node",
        "edge",
        "connect",
        "descriptor_interrupt",
    },
    "_lowering_block_dispatch": {"block"},
    "_lowering_blocks": {
        "_lower_step",
        "_lower_verify",
        "_lower_decide",
        "_lower_accept",
        "_lower_choose",
    },
    "_lowering_parallel": {
        "_parallel_scope_fragment",
        "_lower_parallel_branches",
        "_route_parallel_outcomes",
        "parallel",
    },
    "_lowering_call_planning": {
        "_resolved_call_child",
        "_call_identity",
        "_declare_call_context_channels",
        "_bind_call_artifacts",
        "_declare_call_contract_state",
        "_call_scope_fragment",
        "_call_context_nodes",
    },
    "_lowering_call": {"call"},
    "_lowering_call_bundle": {
        "_load_child_document",
        "_validate_root_child_state",
        "_register_specialized_child_channels",
        "_register_specialized_effect_channels",
        "_specialize_call_members",
        "_compiled_bundle_digest",
        "_record_call_workflow_dependency",
        "_specialized_members_by_source",
        "_reachable_dependency_members",
        "_rebased_dependency_digest",
        "_call_fragment_dependency_digest",
        "_record_call_transitive_dependencies",
        "_call_post_output",
        "_finish_call_graph",
        "_specialize_child",
    },
    "_lowering_artifacts": {"_artifact_producers"},
    "_lowering_graph_plan": {
        "_graph_source",
        "_graph_plan",
        "_closed_graph_components",
        "_closed_graph_boundary",
    },
    "_lowering_graph_rewrite": {
        "_declare_fragment_state",
        "_qualify_fragment_descriptor_state",
        "_qualify_fragment_descriptor_artifacts",
        "_inherit_fragment_scopes",
        "_fragment_descriptor_outcomes",
        "_rewrite_fragment_interrupt",
        "_rewrite_fragment_output",
        "_install_fragment_nodes",
    },
    "_lowering_graph_validation": {
        "_validate_fragment_effects",
        "_install_fragment_edges",
        "_fragment_conditions_are_exhaustive",
        "_validate_fragment_edge_routes",
        "_fragment_loop_analysis",
        "_effect_outcome_reachability",
        "_validate_fragment_effect_outcomes",
        "_reachable_fragment_nodes",
        "_fragment_termination_nodes",
        "_visit_fragment_cycle",
        "_validate_fragment_topology",
    },
    "_lowering_graph": {"_finish_graph_fragment"},
    "_lowering_graph_driver": {"graph"},
    "_lowering_flow": {"repeat", "flow_contract", "build"},
}

HELPER_DEFINITIONS = {
    "_lowering_artifact_matching": {
        "_artifact_producer_candidate",
        "artifact_producer_candidates",
    },
    "_lowering_child_specialization": {
        "_edge_targets",
        "_specialized_fragment_digest",
        "_rewrite_child_state_template",
        "_specialized_child_state",
        "_descriptor_matches_artifact",
        "_specialize_descriptor_logical_id",
        "_specialize_descriptor_runner",
        "_specialize_descriptor_inputs",
        "_specialize_scope_descriptor",
        "_specialize_child_descriptor",
        "_specialize_child_node_state",
        "_managed_brief_content",
        "_managed_brief_identity",
        "_managed_brief",
        "_specialize_child_node",
        "_specialize_child_edges",
        "_specialize_child_loops",
    },
    "_lowering_conditions": {
        "_condition_segments",
        "_rewrite_condition_references",
        "_split_condition_keyword",
        "_condition_may_match_outcome",
    },
    "_lowering_contracts": {
        "LoweredGeneratedFile",
        "LoweredDependency",
        "_Exit",
        "_Fragment",
        "_GraphFragmentPlan",
        "_FragmentNames",
    },
    "_lowering_descriptors": {
        "lower_accept_descriptor",
        "lower_publish_descriptor",
    },
    "_lowering_graph_descriptor": {
        "qualify_fragment_interrupt_channels",
        "protected_fragment_descriptor",
        "_qualify_protected_descriptor",
        "_rewrite_fragment_message",
        "rewrite_fragment_descriptor",
    },
    "_lowering_graph_nodes": {
        "protected_fragment_resume_keys",
        "prepare_fragment_node",
        "store_fragment_node",
    },
    "_lowering_identity": {
        "_stable_id",
        "_fragment_state_namespace",
        "_specialized_state_key",
    },
}

BUILDER_FIELDS = {
    "validated",
    "workflow",
    "catalog",
    "generated_files",
    "dependencies",
    "nodes",
    "edges",
    "state",
    "generated_state_names",
    "loop_limits",
    "loop_exits",
    "source_nodes",
    "outcome_keys",
    "artifact_state_keys",
    "terminals",
    "active_scope_state_keys",
    "outcome_targets",
    "capture_aborted_effects",
    "inside_parallel_branch",
}


def _class_methods(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert len(classes) == 1
    return {
        node.name
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _internal_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module is not None
        and (node.module.startswith("_lowering_") or node.module == "lowering")
    }


def test_lowering_roles_have_exact_single_ownership() -> None:
    owned: set[str] = set()
    for module_name, expected in ROLE_METHODS.items():
        methods = _class_methods(SOURCE / f"{module_name}.py")
        assert methods == expected
        assert owned.isdisjoint(methods)
        owned.update(methods)
    lowering_tree = ast.parse((SOURCE / "lowering.py").read_text(encoding="utf-8"))
    builder = next(
        node
        for node in lowering_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_Builder"
    )
    facade_methods = {
        node.name for node in builder.body if isinstance(node, ast.FunctionDef)
    }
    assert facade_methods == {"__init__"}


def test_lowering_facade_uses_one_shared_instance_dictionary() -> None:
    lowering = importlib.import_module("lockstep.workflow.lowering")
    assert "__slots__" not in lowering._Builder.__dict__
    assert all("__slots__" not in base.__dict__ for base in lowering._Builder.__bases__)
    for methods in ROLE_METHODS.values():
        for name in methods:
            assert inspect.getattr_static(lowering._Builder, name) is not None


def test_lowering_builder_is_the_only_state_initializer() -> None:
    tree = ast.parse((SOURCE / "lowering.py").read_text(encoding="utf-8"))
    builder = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_Builder"
    )
    initializer = next(
        node
        for node in builder.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    fields = {
        target.attr
        for node in ast.walk(initializer)
        for target in (
            node.targets
            if isinstance(node, ast.Assign)
            else (node.target,)
            if isinstance(node, ast.AnnAssign)
            else ()
        )
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    assert fields == BUILDER_FIELDS
    for module_name in ROLE_METHODS:
        assert "__init__" not in _class_methods(SOURCE / f"{module_name}.py")


def test_lowering_helpers_have_exact_cohesive_ownership() -> None:
    assert {path.stem for path in SOURCE.glob("_lowering_*.py")} == {
        *ROLE_METHODS,
        *HELPER_DEFINITIONS,
    }
    for module_name, expected in HELPER_DEFINITIONS.items():
        assert _module_definitions(SOURCE / f"{module_name}.py") == expected


def test_lowering_internal_import_graph_is_complete_acyclic_and_facade_free() -> None:
    module_names = {*ROLE_METHODS, *HELPER_DEFINITIONS}
    dependencies = {
        module_name: _internal_imports(SOURCE / f"{module_name}.py")
        for module_name in module_names
    }
    assert all("lowering" not in imported for imported in dependencies.values())
    assert all(imported <= module_names for imported in dependencies.values())

    remaining = set(module_names)
    while remaining:
        leaves = {
            module_name
            for module_name in remaining
            if not (dependencies[module_name] & remaining)
        }
        assert leaves, f"lowering import cycle: {sorted(remaining)}"
        remaining -= leaves


def test_lowering_public_reexports_preserve_identity_and_type_hints() -> None:
    lowering = importlib.import_module("lockstep.workflow.lowering")
    contracts = importlib.import_module("lockstep.workflow._lowering_contracts")
    descriptors = importlib.import_module("lockstep.workflow._lowering_descriptors")
    for name, owner in {
        "LoweredGeneratedFile": contracts,
        "LoweredDependency": contracts,
        "lower_accept_descriptor": descriptors,
        "lower_publish_descriptor": descriptors,
    }.items():
        assert getattr(lowering, name) is getattr(owner, name)
    assert lowering.__all__ == (
        "LoweredDependency",
        "LoweredGeneratedFile",
        "lower_accept_descriptor",
        "lower_publish_descriptor",
        "lower_workflow",
    )
    assert typing.get_type_hints(lowering.lower_accept_descriptor) == {
        "logical_id": str,
        "artifact_handle": str,
        "producer_result_state_key": str,
        "declared_name": str,
        "destination": str,
        "transformation": typing.Literal["identity"],
        "audience": typing.Literal["local-project"],
        "return": dict[str, typing.Any],
    }
    assert typing.get_type_hints(lowering.lower_publish_descriptor) == {
        "logical_id": str,
        "artifact_handle": str,
        "producer_result_state_key": str,
        "declared_name": str,
        "acceptance_result_state_key": str,
        "destination": str,
        "return": dict[str, typing.Any],
    }
    assert typing.get_type_hints(lowering.lower_workflow) == {
        "validated": lowering.ValidatedWorkflow,
        "catalog": lowering.WorkflowCatalog | None,
        "return": tuple[
            dict[str, typing.Any],
            dict[str, typing.Any],
            tuple[lowering.LoweredGeneratedFile, ...],
            tuple[lowering.LoweredDependency, ...],
        ],
    }
    assert typing.get_type_hints(lowering.LoweredGeneratedFile) == {
        "relative_path": str,
        "content": bytes,
        "sha256": str,
        "logical_name": str,
        "use_pointer": str,
        "definition_sha256": str,
    }
    assert typing.get_type_hints(lowering.LoweredDependency) == {
        "kind": str,
        "logical_name": str,
        "use_pointer": str,
        "definition_sha256": str,
        "compiled_sha256": str,
        "generated_root": str | None,
    }
    assert lowering.LoweredGeneratedFile.__dataclass_params__.frozen is True
    assert lowering.LoweredDependency.__dataclass_params__.frozen is True


def test_lowering_import_orders_are_independent() -> None:
    snippets = (
        (
            "import lockstep.workflow._lowering_child_specialization; "
            "import lockstep.workflow.lowering"
        ),
        (
            "import lockstep.workflow.lowering; "
            "import lockstep.workflow._lowering_child_specialization"
        ),
    )
    for snippet in snippets:
        subprocess.run([sys.executable, "-c", snippet], check=True)
