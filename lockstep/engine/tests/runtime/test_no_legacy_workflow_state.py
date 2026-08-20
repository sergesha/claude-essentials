from __future__ import annotations

import ast
from pathlib import Path

PRODUCTION = Path(__file__).parents[2] / "src" / "lockstep"


def test_production_has_no_legacy_workflow_state_or_raw_recipe_calls():
    forbidden_names = {
        "RunIndex",
        "ACTIVE_STATUS",
        "legacy_compile_recipe",
        "legacy_validate_recipe",
    }
    offenders: list[str] = []
    for path in PRODUCTION.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                offenders.append(f"{path.relative_to(PRODUCTION)}:{node.lineno}:{node.id}")
            if (
                isinstance(node, ast.Constant)
                and node.value == "runs.json"
            ):
                offenders.append(f"{path.relative_to(PRODUCTION)}:{node.lineno}:runs.json")
    assert offenders == []


def test_only_yamlgraph_adapter_imports_native_runtime_packages():
    offenders: list[str] = []
    for path in PRODUCTION.rglob("*.py"):
        if path.name == "yamlgraph_adapter.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(
                    name in {"yamlgraph", "langgraph"}
                    or name.startswith(("yamlgraph.", "langgraph."))
                    for name in names
                ):
                    offenders.append(f"{path.relative_to(PRODUCTION)}:{node.lineno}")
    assert offenders == []
