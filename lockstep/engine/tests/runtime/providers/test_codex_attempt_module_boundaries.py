"""Ownership freeze for the durable Codex attempt driver."""

from __future__ import annotations

import subprocess
import sys


def test_codex_attempt_has_acyclic_owners_and_identity_facade() -> None:
    script = r'''
import ast
import dataclasses
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import typing

prefix = "lockstep.runtime.providers"
support_name = f"{prefix}._codex_support"
services_name = f"{prefix}._codex_services"
preparation_name = f"{prefix}._codex_preparation"
attempt_name = f"{prefix}._codex_attempt"
facade_name = f"{prefix}.codex"

support_definitions = {
    "CodexProviderError",
    "CodexCaptureLimits",
    "_canonical",
    "_sha256_file",
    "_stat_identity",
    "_capture_executable",
    "_credential_identity",
    "_managed_argv",
    "CodexInstallationBinding",
    "CodexLaunchDecisionGate",
    "CodexSandboxAttestor",
    "_attestation_digest",
}
support_methods = {
    "CodexProviderError": set(),
    "CodexCaptureLimits": {"__post_init__"},
    "CodexInstallationBinding": {"capture", "revalidate"},
    "CodexLaunchDecisionGate": {"__init__", "generation", "revoke", "commitment"},
    "CodexSandboxAttestor": {"__init__", "preflight"},
}
service_fields = {
    "_blobs",
    "_clock",
    "_decision_gate",
    "_limits",
    "_sandbox",
    "_workspaces",
}
service_methods = {
    "_CodexAttemptServices": set(),
    "_ServiceAlias": {"__get__", "__set__"},
}
state_methods = {
    "__init__",
    "_directory",
    "_write_once",
    "_atomic_json",
    "_load_record",
    "launch_record",
}
preparation_methods = {
    "_admit_attempt",
    "_input",
    "_assert_no_project_control_surfaces",
    "_recover_prepared_launch",
    "_request_payload",
    "_inner_argv",
    "_execution_cwd",
    "_validate_prepare_request",
    "_recover_existing_preparation",
    "_prepare_workspace",
    "_current_workspace_binding",
    "_provisional_launch_record",
    "_attested_launch_record",
    "_commit_prepared_launch",
}
driver_methods = {
    "_policy",
    "_launcher_binding_digest",
    "prepare",
    "_state",
    "_launch_body",
    "_validate_launch",
    "_supervisor_ready",
    "_supervisor_alive",
    "_commit_ready_supervisor",
    "ensure_started",
    "_alive",
    "_terminal_receipt",
    "_error_result",
    "_stored_result",
    "_cleanup_spools",
    "_parse_result",
    "_finalize_workspace",
    "_terminal",
    "inspect",
    "lookup",
    "cancel",
    "quiesce",
    "wait_terminal",
}
adapter_methods = {
    "__init__",
    "binding_digest",
    "spawn_count",
    "prepare",
    "ensure_started",
    "inspect",
    "cancel",
    "quiesce",
    "__getattr__",
    "__setattr__",
}
state_fields = {
    "_attempts",
    "_binding",
    "_installation",
    "_services",
    "binding_digest",
    "owner_state_dir",
    "spawn_count",
}


def module_tree(module):
    return ast.parse(inspect.getsource(module))


def top_level_definitions(tree):
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def class_methods(tree, class_name):
    class_node = top_level_definitions(tree)[class_name]
    return {
        node.name
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def stored_fields(tree, class_name):
    class_node = top_level_definitions(tree)[class_name]
    return {
        node.attr
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def definition_count(tree):
    return sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in ast.walk(tree)
    )


def self_hook_calls(tree, class_name, method_name, hooks):
    class_node = top_level_definitions(tree)[class_name]
    method = next(
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )
    return {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr in hooks
    }


support = importlib.import_module(support_name)
assert services_name not in sys.modules
assert preparation_name not in sys.modules
assert attempt_name not in sys.modules
assert facade_name not in sys.modules

services = importlib.import_module(services_name)
assert preparation_name not in sys.modules
assert attempt_name not in sys.modules
assert facade_name not in sys.modules

preparation = importlib.import_module(preparation_name)
assert attempt_name not in sys.modules
assert facade_name not in sys.modules

attempt = importlib.import_module(attempt_name)
assert facade_name not in sys.modules

support_tree = module_tree(support)
services_tree = module_tree(services)
preparation_tree = module_tree(preparation)
attempt_tree = module_tree(attempt)

assert set(top_level_definitions(support_tree)) == support_definitions
assert set(top_level_definitions(services_tree)) == {
    "_CodexAttemptServices",
    "_ServiceAlias",
}
assert set(top_level_definitions(preparation_tree)) == {
    "CodexLaunchRecord",
    "_CodexAttemptState",
    "_CodexPreparation",
    "_record_data",
}
assert set(top_level_definitions(attempt_tree)) == {"_CodexAttemptDriver"}
for class_name, methods in support_methods.items():
    assert class_methods(support_tree, class_name) == methods
for class_name, methods in service_methods.items():
    assert class_methods(services_tree, class_name) == methods
assert class_methods(preparation_tree, "CodexLaunchRecord") == set()
assert class_methods(preparation_tree, "_CodexAttemptState") == state_methods
assert class_methods(preparation_tree, "_CodexPreparation") == preparation_methods
assert class_methods(attempt_tree, "_CodexAttemptDriver") == driver_methods

engine_root = Path(inspect.getsourcefile(support)).parents[4]
thresholds = json.loads(
    (engine_root / "tests" / "architecture" / "architecture_thresholds.json").read_bytes()
)["kinds"]["file"]
for tree in (support_tree, services_tree, preparation_tree, attempt_tree):
    assert definition_count(tree) < thresholds["signals"]["definition_count"]
    assert sum(isinstance(node, ast.ClassDef) for node in tree.body) < thresholds["signals"]["class_count"]

assert stored_fields(preparation_tree, "_CodexAttemptState") == state_fields
assert stored_fields(services_tree, "_ServiceAlias") == set()
assert stored_fields(preparation_tree, "_CodexPreparation") == set()
assert stored_fields(attempt_tree, "_CodexAttemptDriver") == {"spawn_count"}
assert {field.name for field in dataclasses.fields(services._CodexAttemptServices)} == service_fields
assert services._CodexAttemptServices.__dataclass_params__.frozen is True
for name in service_fields:
    assert isinstance(
        preparation._CodexAttemptState.__dict__[name], services._ServiceAlias
    )
assert not isinstance(
    preparation._CodexAttemptState.__dict__.get("_installation"),
    services._ServiceAlias,
)

service_values = {name: object() for name in service_fields}
state = object.__new__(preparation._CodexAttemptState)
state._services = services._CodexAttemptServices(**service_values)
assert state._sandbox is service_values["_sandbox"]
before_services = state._services
replacement_limits = object()
state._limits = replacement_limits
assert state._limits is replacement_limits
assert state._services is not before_services
for name in service_fields - {"_limits"}:
    assert getattr(state, name) is service_values[name]
assert attempt._CodexAttemptDriver.__bases__ == (
    preparation._CodexAttemptState,
    preparation._CodexPreparation,
)

pinned_hooks = {
    "_launcher_binding_digest",
    "_request_payload",
    "_inner_argv",
    "_execution_cwd",
    "_parse_result",
}
assert self_hook_calls(
    attempt_tree, "_CodexAttemptDriver", "prepare", pinned_hooks
) == {"_request_payload"}
assert self_hook_calls(
    preparation_tree,
    "_CodexPreparation",
    "_provisional_launch_record",
    pinned_hooks,
) == {"_inner_argv", "_execution_cwd"}
assert self_hook_calls(
    attempt_tree, "_CodexAttemptDriver", "ensure_started", pinned_hooks
) == {"_launcher_binding_digest"}
assert self_hook_calls(
    attempt_tree, "_CodexAttemptDriver", "_terminal", pinned_hooks
) == {"_parse_result"}
for tree in (preparation_tree, attempt_tree):
    assert not {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in pinned_hooks
        and not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        )
    }

record_hints = typing.get_type_hints(preparation.CodexLaunchRecord)
state_init_hints = typing.get_type_hints(preparation._CodexAttemptState.__init__)
service_hints = typing.get_type_hints(services._CodexAttemptServices)
load_hints = typing.get_type_hints(preparation._CodexAttemptState._load_record)
launch_record_hints = typing.get_type_hints(
    preparation._CodexAttemptState.launch_record
)
payload_hints = typing.get_type_hints(preparation._CodexPreparation._request_payload)
argv_hints = typing.get_type_hints(preparation._CodexPreparation._inner_argv)
cwd_hints = typing.get_type_hints(preparation._CodexPreparation._execution_cwd)
provisional_hints = typing.get_type_hints(
    preparation._CodexPreparation._provisional_launch_record
)
attested_hints = typing.get_type_hints(
    preparation._CodexPreparation._attested_launch_record
)
launcher_hints = typing.get_type_hints(
    attempt._CodexAttemptDriver._launcher_binding_digest
)
parse_hints = typing.get_type_hints(attempt._CodexAttemptDriver._parse_result)
prepare_hints = typing.get_type_hints(attempt._CodexAttemptDriver.prepare)

assert record_hints["workspace_path"] is Path
assert record_hints["cwd"] is Path
assert state_init_hints["decision_gate"] is support.CodexLaunchDecisionGate
assert set(service_hints) == service_fields
assert service_hints["_decision_gate"] is support.CodexLaunchDecisionGate
assert service_hints["_limits"] is support.CodexCaptureLimits
assert support.CodexInstallationBinding in typing.get_args(
    state_init_hints["installation"]
)
assert support.CodexCaptureLimits in typing.get_args(state_init_hints["limits"])
assert load_hints["return"] is preparation.CodexLaunchRecord
assert launch_record_hints["return"] is preparation.CodexLaunchRecord
assert payload_hints["request"] is preparation.EffectRequest
assert argv_hints["binding"] is support.CodexInstallationBinding
assert argv_hints["workspace"] is Path
assert argv_hints["request"] is preparation.EffectRequest
assert cwd_hints["workspace"] is Path
assert cwd_hints["request"] is preparation.EffectRequest
assert provisional_hints["binding"] is support.CodexInstallationBinding
assert provisional_hints["return"] is preparation.CodexLaunchRecord
assert attested_hints["provisional"] is preparation.CodexLaunchRecord
assert attested_hints["return"] is preparation.CodexLaunchRecord
assert launcher_hints["binding"] is support.CodexInstallationBinding
assert parse_hints["record"] is preparation.CodexLaunchRecord
assert prepare_hints["request"] is attempt.EffectRequest
assert prepare_hints["return"] is attempt.PreparedLaunch
assert facade_name not in sys.modules

facade = importlib.import_module(facade_name)
facade_tree = module_tree(facade)
assert set(top_level_definitions(facade_tree)) == {"CodexRunnerAdapter"}
assert class_methods(facade_tree, "CodexRunnerAdapter") == adapter_methods
assert definition_count(facade_tree) < thresholds["signals"]["definition_count"]
assert sum(isinstance(node, ast.ClassDef) for node in facade_tree.body) < thresholds["signals"]["class_count"]

for name in support_definitions:
    assert getattr(facade, name) is getattr(support, name)
for name in (
    "_CodexAttemptServices",
    "_ServiceAlias",
):
    assert getattr(facade, name) is getattr(services, name)
for name in (
    "CodexLaunchRecord",
    "_CodexAttemptState",
    "_CodexPreparation",
):
    assert getattr(facade, name) is getattr(preparation, name)
assert facade._CodexAttemptDriver is attempt._CodexAttemptDriver
assert facade.os is support.os is preparation.os is attempt.os
assert facade.subprocess is attempt.subprocess

pinned = importlib.import_module(f"{prefix}.pinned")
strategy = pinned._PinnedCodexStrategy
assert issubclass(strategy, attempt._CodexAttemptDriver)
assert strategy.__mro__[:5] == (
    strategy,
    attempt._CodexAttemptDriver,
    preparation._CodexAttemptState,
    preparation._CodexPreparation,
    object,
)
for hook in (
    "_launcher_binding_digest",
    "_request_payload",
    "_inner_argv",
    "_execution_cwd",
    "_parse_result",
):
    assert getattr(strategy, hook) is strategy.__dict__[hook]
assert pinned._CodexAttemptDriver is attempt._CodexAttemptDriver
assert pinned.CodexInstallationBinding is support.CodexInstallationBinding
assert pinned.CodexLaunchRecord is preparation.CodexLaunchRecord
assert pinned.CodexProviderError is support.CodexProviderError
assert pinned._canonical is support._canonical

sys.path.insert(0, str(engine_root / "tests" / "architecture"))
import test_no_god_methods as architecture

paths = {
    "support": "src/lockstep/runtime/providers/_codex_support.py",
    "services": "src/lockstep/runtime/providers/_codex_services.py",
    "preparation": "src/lockstep/runtime/providers/_codex_preparation.py",
    "attempt": "src/lockstep/runtime/providers/_codex_attempt.py",
    "facade": "src/lockstep/runtime/providers/codex.py",
}
tracked = subprocess.run(
    ("git", "ls-files", "src/lockstep"),
    cwd=engine_root,
    check=True,
    capture_output=True,
    text=True,
).stdout.splitlines()
source_paths = tuple(
    sorted(
        {path for path in tracked if path.endswith(".py")}
        | set(paths.values())
    )
)
files = {path: (engine_root / path).read_bytes() for path in source_paths}
index = architecture.build_source_index(engine_root, source_paths, files)
rule_names = {
    "allowlist": "architecture_effect_free_allowlist.json",
    "primitives": "architecture_effect_primitives.json",
    "lifecycle": "architecture_lifecycle.json",
    "schema": "architecture_metrics.schema.json",
    "thresholds": "architecture_thresholds.json",
}
architecture_root = engine_root / "tests" / "architecture"
rules = {
    name: json.loads((architecture_root / filename).read_bytes())
    for name, filename in rule_names.items()
}
resolutions = architecture.resolve_calls(
    index, rules["allowlist"], rules["primitives"]
)
semantics = architecture.propagate_semantics(
    index,
    resolutions,
    rules["primitives"],
    rules["lifecycle"],
    digest_inputs=architecture.domain_lifecycle.SemanticDigestInputs(
        architecture._canonical_sha256(rules["allowlist"]),
        architecture._canonical_sha256(rules["schema"]),
        architecture._canonical_sha256(rules["thresholds"]),
        "task-12c",
        "v1",
    ),
)
report = architecture.evaluate_candidates(
    index,
    architecture.measure_legacy_metrics(index),
    semantics,
    resolutions,
)
expected_files = {
    paths["support"]: (21, 5),
    paths["services"]: (4, 2),
    paths["preparation"]: (24, 3),
    paths["attempt"]: (24, 1),
    paths["facade"]: (11, 1),
}
for path, (definitions, classes) in expected_files.items():
    metric = report.files[f"{path}::@file"]
    assert (metric.definition_count, metric.class_count) == (definitions, classes)
    assert metric.hard_triggers == ()
    assert metric.candidate is False

state_metric = report.classes[
    f'{paths["preparation"]}::_CodexAttemptState'
]
services_metric = report.classes[
    f'{paths["services"]}::_CodexAttemptServices'
]
alias_metric = report.classes[f'{paths["services"]}::_ServiceAlias']
preparation_metric = report.classes[
    f'{paths["preparation"]}::_CodexPreparation'
]
driver_metric = report.classes[f'{paths["attempt"]}::_CodexAttemptDriver']
assert (
    services_metric.method_count,
    services_metric.public_method_count,
    services_metric.mutable_field_count,
    services_metric.candidate,
) == (0, 0, 0, False)
assert (
    alias_metric.method_count,
    alias_metric.public_method_count,
    alias_metric.mutable_field_count,
    alias_metric.candidate,
) == (2, 0, 0, False)
assert (
    state_metric.method_count,
    state_metric.public_method_count,
    state_metric.mutable_field_count,
    state_metric.candidate,
) == (6, 1, 7, False)
assert (
    preparation_metric.method_count,
    preparation_metric.public_method_count,
    preparation_metric.mutable_field_count,
    preparation_metric.candidate,
) == (14, 0, 0, False)
assert (
    driver_metric.method_count,
    driver_metric.public_method_count,
    driver_metric.mutable_field_count,
    driver_metric.candidate,
) == (23, 7, 1, False)
assert state_metric.hard_triggers == ()
assert preparation_metric.hard_triggers == ()
assert driver_metric.hard_triggers == ()

prepare_one_hop = report.one_hops[
    f'{paths["attempt"]}::_CodexAttemptDriver.prepare::@one_hop'
]
assert (
    prepare_one_hop.helper_count,
    prepare_one_hop.summed_cyclomatic,
    prepare_one_hop.summed_cognitive,
    prepare_one_hop.max_nesting,
    prepare_one_hop.legacy_syntactic_fanout_union,
    prepare_one_hop.candidate,
) == (0, 2, 1, 1, 10, False)
assert prepare_one_hop.hard_triggers == ()
assert report.unresolved_callsites == ()

allowed_existing_candidates = {
    f'{paths["support"]}::CodexInstallationBinding.capture',
    f'{paths["support"]}::CodexSandboxAttestor.preflight',
    f'{paths["attempt"]}::_CodexAttemptDriver.ensure_started',
    f'{paths["attempt"]}::_CodexAttemptDriver._parse_result',
    f'{paths["attempt"]}::_CodexAttemptDriver._terminal',
    f'{paths["attempt"]}::_CodexAttemptDriver.ensure_started::@one_hop',
    f'{paths["attempt"]}::_CodexAttemptDriver._terminal::@one_hop',
}
scoped_candidates = {
    identity
    for metrics in (
        report.functions,
        report.one_hops,
        report.classes,
        report.files,
    )
    for identity, metric in metrics.items()
    if any(identity.startswith(f"{path}::") for path in paths.values())
    and metric.candidate
}
assert scoped_candidates == allowed_existing_candidates
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
