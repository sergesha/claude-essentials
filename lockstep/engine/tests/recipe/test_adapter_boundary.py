import ast
from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def forbidden_dependency_imports(package_root: Path) -> list[str]:
    forbidden = {"yamlgraph", "langgraph"}
    violations = []
    for path in package_root.rglob("*.py"):
        if path == package_root / "recipe/yamlgraph_adapter.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".", 1)[0] in forbidden for name in names):
                violations.append(str(path.relative_to(package_root)))
    return violations


def test_only_adapter_imports_yamlgraph_or_langgraph(repo_root):
    assert forbidden_dependency_imports(repo_root / "engine/src/lockstep") == []
