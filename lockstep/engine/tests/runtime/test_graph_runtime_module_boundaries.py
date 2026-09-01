"""Ownership boundary for GraphRuntime's shared native-app authority."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
import typing
from pathlib import Path

from lockstep.runtime.catalog import RunBinding
from lockstep.runtime.native_models import (
    NativeAppPort,
    NativeLineageProof,
    NativeSnapshot,
)

_VALUES_MODULE = "lockstep.runtime._graph_runtime_values"
_FACADE_MODULE = "lockstep.runtime.graph_runtime"
_BASES = {
    "lockstep.runtime._graph_runtime_lifecycle": (
        "_GraphRuntimeLifecycle",
        (
            "checkpoint_path",
            "_ensure_open",
            "bind",
            "unbind",
            "_bound",
            "binding",
            "close",
            "__enter__",
            "__exit__",
        ),
    ),
    "lockstep.runtime._graph_runtime_guard": (
        "_GraphRuntimeGuard",
        (
            "_app_guard",
            "_invoke",
            "decision_guard",
            "start",
            "ensure_started",
            "snapshot",
            "commitment_guard",
            "_interrupt_lineage",
            "_same_coordinate",
            "resume",
            "stream",
        ),
    ),
    "lockstep.runtime._graph_runtime_lineage": (
        "_GraphRuntimeLineage",
        (
            "history",
            "interrupt_lineage",
            "coordinate_lineage",
            "checkpoint_is_ancestor",
        ),
    ),
}
_METHOD_DIGESTS = {
    "checkpoint_path": "3071a32e366f4b86203cc5573d1ffbf0bcbd878ec42983b467877140fb066117",
    "_ensure_open": "0ffb3aca6719a0ad2b900cd1b9bd9b809f36c7981730bdb86c3fad60a11a5cef",
    "bind": "7fb3be64a11f20139590c5138e7b1fbc8f5d71aeac7fa17be675aebe901d9fe2",
    "unbind": "b8e7809d8d88c92ea5ed9028889e5e2c9e96ec20cacfd5af875fe42684d55a22",
    "_bound": "9b99fe1c70b7127ad81798278c475a77fbc210d991ace433ab398bc2d2fbfd45",
    "_app_guard": "f0b025fb4c0efd8d01bd0fdebeb9ff5ebdff4e26b406d76ce4be6f3f6c247a5e",
    "binding": "a5541ea18da53c3d9e090df7e2ea2721d46d37471d64634b6bdf40f197534f72",
    "_invoke": "2e82737e54d0dceb409ab6704b3ff83a2371d69afabbfe898946c9ec39918738",
    "decision_guard": "7f11f438f6c6fb9e5817964b70bf95f25180e1a8419cf1b2268a07ea8a307a83",
    "start": "6096d9b2eaacda6d3a510d31c893caa0d28a917af79340981e288ea2a0742d66",
    "ensure_started": "020e1d03fd68ca023cf03b463ae28199af3b8f2a1c201b1d4032a7b883a09e51",
    "snapshot": "348a8145162d75f7b72b00d86141ce761f1bc5f180b5589f8c0290e9ecd1ea40",
    "commitment_guard": "6d92f7ce1c1e951af4fe4c91bdf73ab42c59dd02f0fb230b96ffcca9cbeab178",
    "history": "1ff24e98afcf58e1843dcdc06788f2cc66a2a548e4afd26895bb7b17a3e224be",
    "interrupt_lineage": "975c4078a6e9ffcbe1db966be3cbfc2ed0bfff63fcf4bd929a38a5d007f6c4d6",
    "_interrupt_lineage": "cf76d0c3ca16500edc302ef28415ad109d34e2504a3ef4720e2aac3c4dbcde8d",
    "coordinate_lineage": "a0d0860360882ca53e323f12769b6fceeeb975c8ef0ee95094c0d19f66fc6f27",
    "checkpoint_is_ancestor": "dba8e29e81abe8ffb7e39081093e2ea7ad77a33c4eb8d46ea04942dee576eeae",
    "_same_coordinate": "4a890a8c0e9acedd8ace93ada7a0d72252a5e3162b4008c6bff28f8a6d98c9b8",
    "resume": "7cddde0f7c62e7127c766fcaff972fc50ef5043811f98b3195ea85be5890012b",
    "stream": "1677f5272bddeb245c177c890ef568043365398f56b03d4144faffc937a2d4f0",
    "close": "fc9f8bfbecdbe960d567c7410b4249a1638143ffa09e71228eb1b8cfb1b5caea",
    "__enter__": "832b88b8c1eb19e6943dc00f05450604389875a077ed25120d1e9c51d3fa1e44",
    "__exit__": "e95ac5ec062efa7d4ddd56a1b7cb5984781c20d6832b5a04c6a2951911575d30",
}
_INSTANCE_FIELDS = {
    "_apps",
    "_app_factory",
    "_bindings",
    "_bundles",
    "_checkpoint_path",
    "_closed",
    "_closing",
    "_guard_local",
    "_invocations",
    "_leases",
    "_lease_ttl",
    "_lock",
}
_INIT_DIGEST = "ecc7abc33e825f90ea8e45dbe14a5008f83a0f226d319363fe1180dd48d920ab"


def _method_nodes(value: type) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(inspect.getsource(value))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    return {
        node.name: node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _digest(node: ast.AST) -> str:
    def stable_dump(value: object) -> str:
        if isinstance(value, ast.AST):
            fields: list[str] = []
            for name, member in ast.iter_fields(value):
                if not isinstance(value, ast.Constant) and (
                    member is None or member == []
                ):
                    continue
                if isinstance(value, ast.Constant) and name == "kind" and member is None:
                    continue
                fields.append(f"{name}={stable_dump(member)}")
            return f"{type(value).__name__}({', '.join(fields)})"
        if isinstance(value, list):
            return f"[{', '.join(stable_dump(member) for member in value)}]"
        return repr(value)

    return hashlib.sha256(stable_dump(node).encode()).hexdigest()


def test_graph_runtime_has_one_shared_authority_and_three_responsibility_bases() -> None:
    values = importlib.import_module(_VALUES_MODULE)
    bases = []
    owned_methods: dict[str, ast.AST] = {}

    for module_name, (class_name, expected_methods) in _BASES.items():
        module = importlib.import_module(module_name)
        base = getattr(module, class_name)
        bases.append(base)
        methods = _method_nodes(base)
        assert tuple(methods) == expected_methods
        assert "__init__" not in methods
        assert "__slots__" not in vars(base)
        assert not (_INSTANCE_FIELDS & vars(base).keys())
        owned_methods.update(methods)

    facade = importlib.import_module(_FACADE_MODULE)
    runtime = facade.GraphRuntime
    assert runtime.__bases__ == tuple(bases)
    assert tuple(_method_nodes(runtime)) == ("__init__",)
    assert set(owned_methods) == set(_METHOD_DIGESTS)
    assert {name: _digest(node) for name, node in owned_methods.items()} == _METHOD_DIGESTS

    init = _method_nodes(runtime)["__init__"]
    assert _digest(init) == _INIT_DIGEST
    assigned = {
        target.attr
        for node in ast.walk(init)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else (node.target,)
        )
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    assert assigned == _INSTANCE_FIELDS
    assert runtime.__module__ == _FACADE_MODULE
    assert facade.NativeCommitment is values.NativeCommitment
    assert facade.NativeCoordinateRejected is values.NativeCoordinateRejected
    assert facade.RuntimeBindingConflict is values.RuntimeBindingConflict
    assert facade.MAX_HISTORY_SNAPSHOTS is values.MAX_HISTORY_SNAPSHOTS == 1024
    assert facade.MAX_HISTORY_INTERRUPTS is values.MAX_HISTORY_INTERRUPTS == 4096
    instance = object.__new__(runtime)
    assert instance.__dict__ is instance.__dict__
    assert all(base.__dict__.get("__slots__") is None for base in bases)

    hints = {
        "checkpoint_path": typing.get_type_hints(runtime.checkpoint_path.fget),
        "_bound": typing.get_type_hints(runtime._bound),
        "start": typing.get_type_hints(runtime.start),
        "interrupt_lineage": typing.get_type_hints(runtime.interrupt_lineage),
    }
    assert hints["checkpoint_path"]["return"] is Path
    assert hints["_bound"]["return"] == tuple[RunBinding, NativeAppPort]
    assert hints["start"]["return"] is NativeSnapshot
    assert hints["interrupt_lineage"]["return"] == NativeLineageProof | None


def test_graph_runtime_values_are_immutable_and_own_no_runtime_state() -> None:
    values = importlib.import_module(_VALUES_MODULE)
    definitions = {
        node.name: node
        for node in ast.parse(inspect.getsource(values)).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert set(definitions) == {
        "NativeCoordinateRejected",
        "RuntimeBindingConflict",
        "NativeCommitment",
    }
    commitment = inspect.signature(values.NativeCommitment)
    assert tuple(commitment.parameters) == ("binding", "snapshot", "interrupt")
    params = values.NativeCommitment.__dataclass_params__
    assert params.frozen is True
    assert [ast.unparse(item) for item in definitions["NativeCommitment"].decorator_list] == [
        "dataclass(frozen=True)"
    ]
    assert not any(
        isinstance(node, ast.ClassDef) and node.name.startswith("_GraphRuntime")
        for node in ast.parse(inspect.getsource(values)).body
    )


def test_graph_runtime_import_dag_is_acyclic_in_a_fresh_process() -> None:
    payload = json.dumps({"values": _VALUES_MODULE, "bases": list(_BASES), "facade": _FACADE_MODULE})
    script = r'''\
import ast
import importlib
import inspect
import json
import sys

spec = json.loads(sys.argv[1])
values = importlib.import_module(spec["values"])
assert spec["facade"] not in sys.modules
assert not (set(spec["bases"]) & set(sys.modules))
for name in spec["bases"]:
    module = importlib.import_module(name)
    assert spec["facade"] not in sys.modules
    tree = ast.parse(inspect.getsource(module))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert spec["facade"] not in imported
    assert not ((set(spec["bases"]) - {name}) & imported)
facade = importlib.import_module(spec["facade"])
assert facade.GraphRuntime.__bases__ == tuple(
    getattr(sys.modules[name], next(key for key in vars(sys.modules[name]) if key.startswith("_GraphRuntime")))
    for name in spec["bases"]
)
'''
    subprocess.run([sys.executable, "-c", script, payload], check=True)
