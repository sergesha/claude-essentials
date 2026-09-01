import ast
import re
from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


_NATIVE_IMPORT_LINE = re.compile(
    r"^\s*(?:from\s+(?:yamlgraph|langgraph)(?:\.|\s)|"
    r"import\s+(?:yamlgraph|langgraph)(?:\.|\s|$))"
)


def forbidden_dependency_imports(repo_root: Path) -> list[str]:
    forbidden = {"yamlgraph", "langgraph"}
    violations = []
    production_files = sorted((repo_root / "engine/src/lockstep").rglob("*.py"))
    production_files.extend(sorted((repo_root / "engine/scripts").rglob("*.py")))
    adapter = repo_root / "engine/src/lockstep/recipe/yamlgraph_adapter.py"
    for path in production_files:
        if path == adapter:
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
                violations.append(str(path.relative_to(repo_root)))
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            if _NATIVE_IMPORT_LINE.match(line):
                violations.append(f"{path.relative_to(repo_root)}:{line_number}")
    return violations


def test_only_adapter_imports_yamlgraph_or_langgraph_in_production(repo_root):
    assert forbidden_dependency_imports(repo_root) == []
