"""Ownership boundary for bounded run-drive recovery."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
import textwrap

_FACADE = "lockstep.runtime.recovery_driver"
_BASES = {
    "lockstep.runtime._recovery_watch_enumeration": (
        "_RecoveryWatchEnumeration",
        ("_sweep_run_drive_watches", "_watch_pages"),
    ),
    "lockstep.runtime._recovery_watch_admission": (
        "_RecoveryWatchAdmission",
        ("_accepted_from", "_try_drive_run_watch", "_matches_project"),
    ),
    "lockstep.runtime._recovery_watch_drive": (
        "_RecoveryWatchDrive",
        ("_drive_run_watch",),
    ),
    "lockstep.runtime._recovery_watch_inspection": (
        "_RecoveryWatchInspection",
        ("_snapshot_for_run_drive_watch", "_pending_run_drive_descriptor"),
    ),
    "lockstep.runtime._recovery_watch_settlement": (
        "_RecoveryWatchSettlement",
        (
            "_settle_terminal_watch",
            "_settle_after_accepted_drive",
            "_drive_delegated_watch",
        ),
    ),
}
_SUPPORT = (
    "lockstep.runtime._recovery_watch_errors",
    "lockstep.runtime._recovery_backfill",
    *_BASES,
)
_UNCHANGED_METHOD_DIGESTS = {
    "_sweep_run_drive_watches": "1290693a16226accf20c9787e7889cb712ff90f5da019512d6042b42ec385f5d",
    "_accepted_from": "0cef40c1dc54d538a30d28fcd8fb259fe2ac5ceb719addc98ede6f972edc9d2c",
    "_try_drive_run_watch": "f7750e341f359dbb252fb28cddf49bb87613d815d38e13b041ecd547cc584ccf",
    "_watch_pages": "6f6a9bb7aee602c1f357291084a9768c715dac93ec7caaa7c382c46989c822e1",
    "_matches_project": "02eff61ec0e46bf00f9b70a0c1d252862cddb9dce363f5bb56873509b09789c3",
    "_settle_terminal_watch": "d5cd14a8bc929be8543d48a259d805c40781682ac7faeb4690c0da3bf8ad97e6",
    "_settle_after_accepted_drive": "e5b86cd1fbde16ce01d8e44b1579414e77109667e5b60e34d3d6f629b76e6f93",
}
_INIT_DIGEST = "1232c8af545b05b7f3344ea5fd7531513c74a2a35d1b60f1911eb395a1fb401d"
_INSTANCE_FIELDS = {
    "_backfill",
    "_blobs",
    "_catalog",
    "_coordinator",
    "_drive_recovered_run",
    "_effects",
    "_exclude_run_drive",
    "_runtime",
    "_snapshot_resolver",
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


def _digest(node: ast.AST) -> str:
    def stable(value: object) -> str:
        if isinstance(value, ast.AST):
            fields = []
            for name, member in ast.iter_fields(value):
                if not isinstance(value, ast.Constant) and (
                    member is None or member == []
                ):
                    continue
                if isinstance(value, ast.Constant) and name == "kind" and member is None:
                    continue
                fields.append(f"{name}={stable(member)}")
            return f"{type(value).__name__}({', '.join(fields)})"
        if isinstance(value, list):
            return f"[{', '.join(stable(member) for member in value)}]"
        return repr(value)

    return hashlib.sha256(stable(node).encode()).hexdigest()


def _calls(node: ast.AST) -> tuple[str, ...]:
    calls = []

    class ExecutionOrder(ast.NodeVisitor):
        def visit_Call(self, item: ast.Call) -> None:
            self.visit(item.func)
            for argument in item.args:
                self.visit(argument)
            for keyword in item.keywords:
                self.visit(keyword.value)
            calls.append(ast.unparse(item.func))

    ExecutionOrder().visit(node)
    return tuple(calls)


def test_recovery_driver_has_five_stateless_responsibility_bases() -> None:
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
        assert set(vars(base)) <= {
            "__module__",
            "__doc__",
            "__dict__",
            "__weakref__",
            *expected,
        }
        bases.append(base)
        owned.update(methods)

    facade = importlib.import_module(_FACADE)
    driver = facade.RecoveryDriver
    assert driver.__bases__ == tuple(bases)
    assert tuple(_methods(driver)) == ("__init__",)
    assert _digest(_methods(driver)["__init__"]) == _INIT_DIGEST
    assert {
        name: _digest(owned[name]) for name in _UNCHANGED_METHOD_DIGESTS
    } == _UNCHANGED_METHOD_DIGESTS

    assignments = {
        target.attr
        for node in ast.walk(_methods(driver)["__init__"])
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    assert assignments == _INSTANCE_FIELDS
    backfill = importlib.import_module("lockstep.runtime._recovery_backfill")
    assert facade._RunDriveBackfill is backfill._RunDriveBackfill
    assert facade._bound_runtime is backfill._bound_runtime


def test_drive_helpers_preserve_replay_guard_and_settlement_order() -> None:
    inspection = importlib.import_module("lockstep.runtime._recovery_watch_inspection")
    settlement = importlib.import_module("lockstep.runtime._recovery_watch_settlement")
    drive = importlib.import_module("lockstep.runtime._recovery_watch_drive")

    snapshot_calls = _calls(
        _methods(inspection._RecoveryWatchInspection)["_snapshot_for_run_drive_watch"]
    )
    snapshot_method = _methods(inspection._RecoveryWatchInspection)[
        "_snapshot_for_run_drive_watch"
    ]
    assert ast.unparse(snapshot_method.body[0].value.func) == "self._runtime.snapshot"
    assert ast.unparse(snapshot_method.body[1].test) == "snapshot.checkpoint_id"
    assert isinstance(snapshot_method.body[1].body[0], ast.Return)
    assert ast.unparse(snapshot_method.body[1].body[0].value) == "snapshot"
    assert ast.unparse(snapshot_method.body[2].test) == (
        "watch.input_blob_sha256 is None or watch.input_blob_size is None"
    )
    assert snapshot_calls.index("self._snapshot_resolver.start_ref") < snapshot_calls.index(
        "self._blobs.read"
    ) < snapshot_calls.index("decode_canonical_start_input") < snapshot_calls.index(
        "self._runtime.ensure_started"
    )
    delegated_calls = _calls(
        _methods(settlement._RecoveryWatchSettlement)["_drive_delegated_watch"]
    )
    assert delegated_calls == (
        "self._drive_recovered_run",
        "self._settle_after_accepted_drive",
    )
    delegated = _methods(settlement._RecoveryWatchSettlement)["_drive_delegated_watch"]
    assert ast.unparse(delegated.body[1].test) == "accepted"
    assert ast.unparse(delegated.body[1].body[0].value.func) == (
        "self._settle_after_accepted_drive"
    )

    drive_method = _methods(drive._RecoveryWatchDrive)["_drive_run_watch"]
    drive_calls = _calls(drive_method)
    assert drive_calls[0] == "self._catalog.get"
    assert drive_calls[-1] == "self._drive_delegated_watch"
    guard = next(node for node in drive_method.body if isinstance(node, ast.With))
    guarded_calls = _calls(guard)
    assert guarded_calls[0] == "_bound_runtime"
    assert "self._snapshot_for_run_drive_watch" in guarded_calls
    assert "self._pending_run_drive_descriptor" in guarded_calls
    assert "self._coordinator.reconcile_one" in guarded_calls
    assert "self._settle_terminal_watch" in guarded_calls
    assert "self._settle_after_accepted_drive" in guarded_calls
    assert "self._drive_delegated_watch" not in guarded_calls
    assert guarded_calls.index("self._snapshot_for_run_drive_watch") < guarded_calls.index(
        "self._settle_terminal_watch"
    ) < guarded_calls.index("self._pending_run_drive_descriptor") < guarded_calls.index(
        "self._coordinator.reconcile_one"
    ) < guarded_calls.index("self._settle_after_accepted_drive")

    direct_settlement = [
        node
        for node in ast.walk(guard)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "accepted"
        and _calls(node) == ("self._settle_after_accepted_drive",)
    ]
    assert len(direct_settlement) == 1
    assert ast.unparse(direct_settlement[0].body[0].value.func) == (
        "self._settle_after_accepted_drive"
    )
    assert ast.unparse(drive_method.body[-1].value.func) == "self._drive_delegated_watch"


def test_recovery_driver_import_dag_is_fresh_and_acyclic() -> None:
    payload = json.dumps({
        "bases": list(_BASES),
        "support": list(_SUPPORT),
        "facade": _FACADE,
        "allowed": {
            "lockstep.runtime._recovery_backfill": [
                "lockstep.runtime._recovery_watch_errors"
            ],
            "lockstep.runtime._recovery_watch_admission": [
                "lockstep.runtime._recovery_watch_errors"
            ],
            "lockstep.runtime._recovery_watch_drive": [
                "lockstep.runtime._recovery_backfill"
            ],
            "lockstep.runtime._recovery_watch_settlement": [
                "lockstep.runtime._recovery_backfill"
            ],
        },
    })
    script = r'''\
import ast
import importlib
import inspect
import json
import sys

spec = json.loads(sys.argv[1])
mode = sys.argv[2]
if mode == "facade-first":
    facade = importlib.import_module(spec["facade"])
for name in spec["support"]:
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
    support_imports = (set(spec["support"]) - {name}) & imported
    assert support_imports == set(spec["allowed"].get(name, ()))
facade = importlib.import_module(spec["facade"])
assert facade.RecoveryDriver.__bases__ == tuple(
    getattr(sys.modules[name], next(
        key for key in vars(sys.modules[name]) if key.startswith("_RecoveryWatch")
    ))
    for name in spec["bases"]
)
'''
    for mode in ("support-first", "facade-first"):
        subprocess.run([sys.executable, "-c", script, payload, mode], check=True)
