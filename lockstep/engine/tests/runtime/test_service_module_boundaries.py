"""Shared-state ownership boundary for the R4 command-service facade."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
import textwrap

import pytest

_BASES = {
    "lockstep.runtime._service_interrupt_descriptors": (
        "_ServiceInterruptDescriptors",
        ("_protected_interrupt_descriptor", "_protected_descriptor"),
    ),
    "lockstep.runtime._service_effect_drive": (
        "_ServiceEffectDrive",
        (
            "_reserve_effect_run",
            "_reserve_effect_run_owned",
            "_activate_effect_run",
            "_deactivate_effect_run",
            "_release_failed_start_reservation",
            "_finish_owned_effect_binding",
            "_release_inactive_effect_binding",
            "_take_active_effect_runs",
            "_drive_engine_owned",
            "_drive_recovered_run",
            "_engine_drive_service",
            "_drain_completion_runs",
        ),
    ),
    "lockstep.runtime._service_composition": (
        "_ServiceComposition",
        (
            "_open_writable_stores",
            "_open_graph_runtime",
            "_effect_coordinator_for",
            "_install_runtime_execution",
            "_open_effect_coordinator",
            "_reconstruct_runtime_execution_context",
            "_install_recovered_runtime_execution",
            "_require_owner_runtime_policy",
        ),
    ),
    "lockstep.runtime._service_writable_core": (
        "_ServiceWritableCore",
        (
            "_prepare_writable_core",
            "_rollback_writable_core_activation",
            "_configure_runtime_execution",
        ),
    ),
    "lockstep.runtime._service_recovery_pump": (
        "_ServiceRecoveryPump",
        (
            "_recover_engine_effects",
            "_completion_pump",
            "_check_completion_pump",
        ),
    ),
    "lockstep.runtime._service_activation_lifecycle": (
        "_ServiceActivationLifecycle",
        (
            "_finish_writable_core_activation",
            "_activate_writable_core",
            "scenario_recover",
            "close",
        ),
    ),
    "lockstep.runtime._service_preflight": (
        "_ServiceRecipeLookup",
        ("_recipe_path", "recipe_path"),
    ),
    "lockstep.runtime._service_start": (
        "_ServiceStart",
        (
            "start",
            "_plan_start",
            "_canonical_start_plan",
            "start_authorized",
            "_start_planned",
            "_authorized_start_service",
        ),
    ),
    "lockstep.runtime._service_session": (
        "_ServiceSession",
        (
            "_existing_run",
            "_preflight_session_readonly",
            "_bind_existing",
            "require_session",
            "_require_session_owner",
        ),
    ),
    "lockstep.runtime._service_worker": (
        "_ServiceWorker",
        (
            "_snapshot_status",
            "_worker_interrupt",
            "_resume_worker",
            "scenario_done",
            "_pending_acceptance",
            "scenario_escalate",
            "scenario_abort",
            "done",
            "escalate",
            "abort",
        ),
    ),
    "lockstep.runtime._service_publication_consent": (
        "_ServicePublicationConsent",
        (
            "preview_publication_consent",
            "issue_publication_consent",
            "scenario_accept_artifact",
            "revoke_publication_consents",
        ),
    ),
}
_SUPPORT = {
    "lockstep.runtime._service_payloads": (
        "validate_evidence_payload",
        "validate_evidence_shape",
        "validate_reason_payload",
    ),
    "lockstep.runtime._service_preflight": (
        "_resolve_preflight_recipe",
        "preflight_recipe",
    ),
}
_BODY_DIGEST = "adc308667337152352127e56aeae6fd0b31ecb85f4c90dafbba4ecb2ed584e00"
_BODY_EXCEPTIONS = {
    "_completion_pump",
    "_drain_completion_runs",
    "_install_runtime_execution",
    "_open_writable_stores",
    "_plan_start",
    "start_authorized",
}
_INSTANCE_FIELDS = {
    "_activation_lock",
    "_active_effect_lock",
    "_active_effect_queue",
    "_active_effect_runs",
    "_admission_recovery_lock",
    "_closed",
    "_initial_recovery_exclusion",
    "_owned_effect_bindings",
    "_pump_failure",
    "_pump_stop",
    "_pump_thread",
    "_pump_wakeup",
    "_queued_effect_runs",
    "_recovery_driver",
    "_runtime_execution_composition",
    "_runtime_execution_context",
    "_start_activation",
    "_writable_core_active",
    "authority_policy",
    "recipes_dir",
    "state_dir",
}


def _methods(value: type) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    owner = next(
        node
        for node in ast.parse(textwrap.dedent(inspect.getsource(value))).body
        if isinstance(node, ast.ClassDef)
    )
    return {
        node.name: node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _body_digest(methods: dict[str, ast.AST]) -> str:
    payload = {
        name: hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
        for name, node in methods.items()
        if name not in _BODY_EXCEPTIONS
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def test_service_facade_has_exact_stateless_capability_bases() -> None:
    bases = []
    owned = {}
    for module_name, (class_name, expected) in _BASES.items():
        module = importlib.import_module(module_name)
        base = getattr(module, class_name)
        methods = _methods(base)
        assert tuple(methods) == expected
        assert base.__bases__ == (object,)
        assert "__init__" not in methods
        assert "__slots__" not in vars(base)
        assert not (_INSTANCE_FIELDS & vars(base).keys())
        bases.append(base)
        owned.update(methods)

    service = importlib.import_module("lockstep.runtime.service")
    facade = service.LockstepCommandService
    assert facade.__bases__ == tuple(bases)
    assert tuple(_methods(facade)) == ("__init__",)
    owned.update(_methods(facade))
    assert _body_digest(owned) == _BODY_DIGEST

    assignments = {
        target.attr
        for node in ast.walk(owned["__init__"])
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    assert assignments == _INSTANCE_FIELDS
    assert set(vars(facade)) <= {
        "__module__",
        "__doc__",
        "__init__",
        "_MAX_ACTIVE_EFFECT_RUNS",
        "_MAX_ENGINE_PROGRESS_DECISIONS",
        "__dict__",
        "__weakref__",
    }


def test_service_support_functions_keep_facade_identity() -> None:
    service = importlib.import_module("lockstep.runtime.service")
    for module_name, names in _SUPPORT.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert getattr(service, name) is getattr(module, name)


def test_class_qualified_descriptor_dispatch_uses_public_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = importlib.import_module("lockstep.runtime.service")
    descriptors = importlib.import_module(
        "lockstep.runtime._service_interrupt_descriptors"
    )
    assert descriptors.LockstepCommandService is service.LockstepCommandService

    def replaced(_interrupt):
        raise RuntimeError("public facade dispatch")

    monkeypatch.setattr(
        service.LockstepCommandService,
        "_protected_interrupt_descriptor",
        staticmethod(replaced),
    )
    with pytest.raises(RuntimeError, match="public facade dispatch"):
        service.LockstepCommandService._protected_descriptor(object())


def test_command_service_never_inherits_observation_backdoors() -> None:
    from lockstep.runtime.service import LockstepCommandService

    assert not {
        "scenario_status",
        "scenario_wait",
        "scenario_history",
        "scenario_events",
        "list_runs",
        "run_trace",
    } & set(dir(LockstepCommandService))


def test_service_import_dag_is_acyclic_in_fresh_processes() -> None:
    modules = tuple(dict.fromkeys((*_SUPPORT, *_BASES, "lockstep.runtime._service_values")))
    payload = json.dumps(
        {
            "facade": "lockstep.runtime.service",
            "modules": modules,
            "allowed": {
                "lockstep.runtime._service_start": [
                    "lockstep.runtime._service_preflight",
                    "lockstep.runtime._service_values",
                ],
                "lockstep.runtime._service_worker": [
                    "lockstep.runtime._service_payloads"
                ],
                "lockstep.runtime._service_writable_core": [
                    "lockstep.runtime._service_values"
                ],
                "lockstep.runtime._service_recovery_pump": [
                    "lockstep.runtime._service_values"
                ],
                "lockstep.runtime._service_activation_lifecycle": [
                    "lockstep.runtime._service_values"
                ],
            },
        }
    )
    script = r'''\
import ast
import importlib
import inspect
import json
import sys

spec = json.loads(sys.argv[1])
mode = sys.argv[2]
if mode == "facade-first":
    importlib.import_module(spec["facade"])
for name in spec["modules"]:
    module = importlib.import_module(name)
    if mode == "support-first":
        assert spec["facade"] not in sys.modules
    imported = set()
    for node in ast.parse(inspect.getsource(module)).body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert spec["facade"] not in imported
    sibling_imports = (set(spec["modules"]) - {name}) & imported
    assert sibling_imports == set(spec["allowed"].get(name, ()))
facade = importlib.import_module(spec["facade"])
assert facade.LockstepCommandService.__bases__ == tuple(
    getattr(sys.modules[module_name], class_name)
    for module_name, (class_name, _methods) in BASES.items()
)
'''.replace("BASES", repr(_BASES))
    for mode in ("support-first", "facade-first"):
        subprocess.run([sys.executable, "-c", script, payload, mode], check=True)


def test_completion_pump_keeps_drain_before_recovery_and_exact_failure_boundary() -> None:
    lifecycle = _methods(
        importlib.import_module(
            "lockstep.runtime._service_recovery_pump"
        )._ServiceRecoveryPump
    )["_completion_pump"]
    loop = next(node for node in lifecycle.body if isinstance(node, ast.While))
    assert ast.unparse(loop.test) == "not self._pump_stop.is_set()"
    assert ast.unparse(loop.body[0]) == (
        "explicitly_woken = self._pump_wakeup.wait(0.25)"
    )
    assert ast.unparse(loop.body[1]) == "self._pump_wakeup.clear()"
    guarded = next(node for node in loop.body if isinstance(node, ast.Try))
    assert ast.unparse(guarded.body[0]) == (
        "active_run_ids = self._drain_completion_runs()"
    )
    assert "self._recover_engine_effects()" in ast.unparse(guarded.body[1])
    assert len(guarded.handlers) == 1
    assert ast.unparse(guarded.handlers[0].type) == "Exception"

    drain = _methods(
        importlib.import_module(
            "lockstep.runtime._service_effect_drive"
        )._ServiceEffectDrive
    )["_drain_completion_runs"]
    guarded_drain = next(node for node in drain.body if isinstance(node, ast.With))
    assert ast.unparse(guarded_drain.items[0].context_expr) == (
        "self._admission_recovery_lock"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(guarded_drain)
        if isinstance(node, ast.Call)
    ]
    assert calls[:4] == [
        "self._take_active_effect_runs",
        "self.catalog.get",
        "self.runtime.bind",
        "self._drive_engine_owned",
    ]
